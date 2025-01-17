from __future__ import annotations

import signal

from aleo_explorer_rust import get_value_id

from aleo_types import *
from explorer.types import Message as ExplorerMessage
from .base import DatabaseBase
from .block import DatabaseBlock


class DatabaseUtil(DatabaseBase):

    @staticmethod
    def get_addresses_from_struct(plaintext: StructPlaintext):
        addresses: set[str] = set()
        for _, p in plaintext.members:
            if isinstance(p, LiteralPlaintext) and p.literal.type == Literal.Type.Address:
                addresses.add(str(p.literal.primitive))
            elif isinstance(p, StructPlaintext):
                addresses.update(DatabaseUtil.get_addresses_from_struct(p))
        return addresses

    @staticmethod
    def get_primitive_from_argument_unchecked(argument: Argument):
        plaintext = cast(PlaintextArgument, cast(PlaintextArgument, argument).plaintext)
        literal = cast(LiteralPlaintext, plaintext).literal
        return literal.primitive

    # debug method
    async def clear_database(self):
        async with self.pool.connection() as conn:
            try:
                await conn.execute("TRUNCATE TABLE block RESTART IDENTITY CASCADE")
                await conn.execute("TRUNCATE TABLE mapping RESTART IDENTITY CASCADE")
                await conn.execute("TRUNCATE TABLE committee_history RESTART IDENTITY CASCADE")
                await conn.execute("TRUNCATE TABLE committee_history_member RESTART IDENTITY CASCADE")
                await conn.execute("TRUNCATE TABLE leaderboard RESTART IDENTITY CASCADE")
                await conn.execute("TRUNCATE TABLE mapping_bonded_history RESTART IDENTITY CASCADE")
                await conn.execute("TRUNCATE TABLE ratification_genesis_balance RESTART IDENTITY CASCADE")
                await self.redis.flushall()
            except Exception as e:
                await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                raise

    async def revert_to_last_backup(self):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    try:
                        redis_keys = [
                            "credits.aleo:bonded",
                            "credits.aleo:committee",
                            "address_stake_reward",
                            "address_transfer_in",
                            "address_transfer_out",
                            "address_fee",
                        ]
                        cursor, keys = await self.redis.scan(0, f"{redis_keys[0]}:history:*", 100)
                        if cursor != 0:
                            raise RuntimeError("unsupported configuration")
                        if not keys:
                            raise RuntimeError("no backup found")
                        keys = sorted(keys, key=lambda x: int(x.split(":")[-1]))
                        last_backup = keys[-1]
                        last_backup_height = int(last_backup.split(":")[-1])
                        for redis_key in redis_keys:
                            if not await self.redis.exists(f"{redis_key}:history:{last_backup_height}"):
                                raise RuntimeError(f"backup key not found: {redis_key}:history:{last_backup_height}")
                        print(f"reverting to last backup: {last_backup_height}")

                        await cur.execute(
                            "select distinct on (mapping_id, key_id) mapping_id, key_id, key, value from mapping_history "
                            "where height <= %s "
                            "order by mapping_id, key_id, id desc",
                            (last_backup_height,)
                        )
                        mapping_snapshot = await cur.fetchall()
                        await cur.execute(
                            "TRUNCATE TABLE mapping_value RESTART IDENTITY"
                        )
                        for item in mapping_snapshot:
                            mapping_id = item["mapping_id"]
                            key_id = item["key_id"]
                            key = item["key"]
                            value = item["value"]
                            if value is not None:
                                value_id = get_value_id(key_id, value)
                                await cur.execute(
                                    "INSERT INTO mapping_value (mapping_id, key_id, value_id, key, value) "
                                    "VALUES (%s, %s, %s, %s, %s) ",
                                    (mapping_id, key_id, value_id, key, value)
                                )
                        await cur.execute(
                            "DELETE FROM mapping_history WHERE height > %s",
                            (last_backup_height,)
                        )

                        blocks_to_revert = await DatabaseBlock.get_full_block_range(u32.max, last_backup_height, conn)
                        for block in blocks_to_revert:
                            for ct in block.transactions:
                                t = ct.transaction
                                # revert to unconfirmed transactions
                                if isinstance(ct, (RejectedDeploy, RejectedExecute)):
                                    await cur.execute(
                                        "SELECT original_transaction_id FROM transaction WHERE transaction_id = %s",
                                        (str(t.id),)
                                    )
                                    if (res := await cur.fetchone()) is None:
                                        raise RuntimeError(f"missing transaction: {t.id}")
                                    original_transaction_id = res["original_transaction_id"]
                                    if original_transaction_id is not None:
                                        if isinstance(ct, RejectedDeploy):
                                            original_type = "Deploy"
                                        else:
                                            original_type = "Execute"
                                        await cur.execute(
                                            "UPDATE transaction SET "
                                            "transaction_id = %s, "
                                            "original_transaction_id = NULL, "
                                            "confimed_transaction_id = NULL,"
                                            "type = %s "
                                            "WHERE transaction_id = %s",
                                            (original_transaction_id, original_type, str(t.id))
                                        )
                                else:
                                    await cur.execute(
                                        "UPDATE transaction SET confimed_transaction_id = NULL WHERE transaction_id = %s",
                                        (str(t.id),)
                                    )
                                # decrease program called counter
                                if isinstance(t, ExecuteTransaction):
                                    transitions = list(t.execution.transitions)
                                    if t.additional_fee.value is not None:
                                        transitions.append(t.additional_fee.value.transition)
                                elif isinstance(t, DeployTransaction):
                                    transitions = [t.fee.transition]
                                    program = t.deployment.program
                                    await cur.execute(
                                        "DELETE FROM program WHERE program_id = %s",
                                        (str(program.id),)
                                    )
                                    await cur.execute(
                                        "DELETE FROM mapping WHERE program_id = %s",
                                        (str(program.id),)
                                    )
                                elif isinstance(t, FeeTransaction):
                                    if isinstance(ct, RejectedDeploy):
                                        transitions = [t.fee.transition]
                                    elif isinstance(ct, RejectedExecute):
                                        rejected = ct.rejected
                                        if not isinstance(rejected, RejectedExecution):
                                            raise RuntimeError("wrong transaction data")
                                        transitions = list(rejected.execution.transitions)
                                        transitions.append(t.fee.transition)
                                    else:
                                        raise RuntimeError("wrong transaction type")
                                for ts in transitions:
                                    await cur.execute(
                                        "UPDATE program_function pf SET called = called - 1 "
                                        "FROM program p "
                                        "WHERE p.program_id = %s AND p.id = pf.program_id AND pf.name = %s",
                                        (str(ts.program_id), str(ts.function_name))
                                    )
                            # revert leaderboard
                            await cur.execute(
                                "SELECT address, reward FROM prover_solution ps "
                                "JOIN coinbase_solution cs on ps.coinbase_solution_id = cs.id "
                                "JOIN explorer.block b on b.id = cs.block_id "
                                "WHERE b.height = %s",
                                (block.height,)
                            )
                            for item in await cur.fetchall():
                                address = item["address"]
                                reward = item["reward"]
                                await cur.execute(
                                    "UPDATE leaderboard SET total_reward = total_reward - %s WHERE address = %s",
                                    (reward, address)
                                )
                        await cur.execute(
                            "DELETE FROM block WHERE height > %s",
                            (last_backup_height,)
                        )
                        await cur.execute(
                            "DELETE FROM committee_history WHERE height > %s",
                            (last_backup_height,)
                        )

                        for redis_key in redis_keys:
                            backup_key = f"{redis_key}:history:{last_backup_height}"
                            await self.redis.copy(backup_key, redis_key, replace=True)
                            await self.redis.persist(redis_key)
                            # remove rollback backup as well
                            _, keys = await self.redis.scan(0, f"{redis_key}:rollback_backup:*", 100)
                            for key in keys:
                                await self.redis.delete(key)

                    except Exception as e:
                        await self.message_callback(ExplorerMessage(ExplorerMessage.Type.DatabaseError, e))
                        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
                        raise
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})