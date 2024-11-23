# Copyright 2022 Amethyst Reese
# Licensed under the MIT license

"""
Core implementation of aiosqlite proxies
"""

import asyncio
import logging
import sqlite3
from functools import partial
from pathlib import Path
from queue import Empty, Queue, SimpleQueue
from threading import Thread
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Literal,
    Optional,
    Tuple,
    Type,
    Union,
)
from warnings import warn

from .context import contextmanager
from .cursor import Cursor

import asyncio
import aiorpcx


__all__ = ["connect", "Connection", "Cursor"]

LOG = logging.getLogger("aiosqliteremot")


IsolationLevel = Optional[Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"]]


def set_result(fut: asyncio.Future, result: Any) -> None:
    """Set the result of a future if it hasn't been set already."""
    if not fut.done():
        fut.set_result(result)


def set_exception(fut: asyncio.Future, e: BaseException) -> None:
    """Set the exception of a future if it hasn't been set already."""
    if not fut.done():
        fut.set_exception(e)


_STOP_RUNNING_SENTINEL = object()


class DBServerConnectionHandle:
    def __init__(
        self,
        connector: Callable[[], sqlite3.Connection], # no longer a sqlite3.Connection but a rpc connection
        iter_chunk_size: int,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:

        pass

    
class Connection(Thread):
    def __init__(
        self,
        connector: Callable[[], sqlite3.Connection], # no longer a sqlite3.Connection but a rpc connection
        iter_chunk_size: int,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()
        self._running = True
        self._connection: Optional[sqlite3.Connection] = None
        self._connector = connector
        self._tx: SimpleQueue[Tuple[asyncio.Future, Callable[[], Any]]] = SimpleQueue()
        self._iter_chunk_size = iter_chunk_size

        if loop is not None:
            warn(
                "aiosqlite.Connection no longer uses the `loop` parameter",
                DeprecationWarning,
            )

    def _stop_running(self):
        self._running = False
        # PEP 661 is not accepted yet, so we cannot type a sentinel
        self._tx.put_nowait(_STOP_RUNNING_SENTINEL)  # type: ignore[arg-type]

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ValueError("no active connection")

        return self._connection

    def _execute_insert(self, sql: str, parameters: Any) -> Optional[sqlite3.Row]:
        cursor = self._conn.execute(sql, parameters)
        cursor.execute("SELECT last_insert_rowid()")
        return cursor.fetchone()

    def _execute_fetchall(self, sql: str, parameters: Any) -> Iterable[sqlite3.Row]:
        cursor = self._conn.execute(sql, parameters)
        return cursor.fetchall()

    def run(self) -> None:
        """
        Execute function calls on a separate thread.

        :meta private:
        """
        while True:
            # Continues running until all queue items are processed,
            # even after connection is closed (so we can finalize all
            # futures)

            tx_item = self._tx.get()
            if tx_item is _STOP_RUNNING_SENTINEL:
                break

            future, function = tx_item

            try:
                LOG.debug("executing %s", function)
                result = function()
                LOG.debug("operation %s completed", function)
                future.get_loop().call_soon_threadsafe(set_result, future, result)
            except BaseException as e:  # noqa B036
                LOG.debug("returning exception %s", e)
                future.get_loop().call_soon_threadsafe(set_exception, future, e)

    async def _execute(self, fn, *args, **kwargs):
        """Queue a function with the given arguments for execution."""
        if not self._running or not self._connection:
            raise ValueError("Connection closed")

        function = partial(fn, *args, **kwargs)
        future = asyncio.get_event_loop().create_future()

        self._tx.put_nowait((future, function))

        return await future

    async def _connect(self) -> "Connection":
        """Connect to the actual sqlite database."""

        self._connection = await self._connector()
        # if self._connection is None:
        #     try:
        #         future = asyncio.get_event_loop().create_future()
        #         self._tx.put_nowait((future, self._connector))
        #         self._connection = await future
        #     except Exception:
        #         self._stop_running()
        #         self._connection = None
        #         raise

        return self
        

    def __await__(self) -> Generator[Any, None, "Connection"]:
        self.start()
        return self._connect().__await__()

    async def __aenter__(self) -> "Connection":
        return await self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    @contextmanager
    async def cursor(self) -> Cursor:
        """Create an aiosqlite cursor wrapping a sqlite3 cursor object."""
        return Cursor(self, await self._execute(self._conn.cursor))

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._execute(self._conn.commit)

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._execute(self._conn.rollback)

    async def close(self) -> None:
        """Complete queued queries/cursors and close the connection."""

        if self._connection is None:
            return

        try:
            await self._execute(self._conn.close)
        except Exception:
            LOG.info("exception occurred while closing connection")
            raise
        finally:
            self._stop_running()
            self._connection = None

    @contextmanager
    async def execute(
        self, sql: str, parameters: Optional[Iterable[Any]] = None
    ) -> Cursor:
        """Helper to create a cursor and execute the given query."""
        if parameters is None:
            parameters = []
        cursor = await self._execute(self._conn.execute, sql, parameters)
        return Cursor(self, cursor)

    @contextmanager
    async def execute_insert(
        self, sql: str, parameters: Optional[Iterable[Any]] = None
    ) -> Optional[sqlite3.Row]:
        """Helper to insert and get the last_insert_rowid."""
        if parameters is None:
            parameters = []
        return await self._execute(self._execute_insert, sql, parameters)

    @contextmanager
    async def execute_fetchall(
        self, sql: str, parameters: Optional[Iterable[Any]] = None
    ) -> Iterable[sqlite3.Row]:
        """Helper to execute a query and return all the data."""
        if parameters is None:
            parameters = []
        return await self._execute(self._execute_fetchall, sql, parameters)

    @contextmanager
    async def executemany(
        self, sql: str, parameters: Iterable[Iterable[Any]]
    ) -> Cursor:
        """Helper to create a cursor and execute the given multiquery."""
        cursor = await self._execute(self._conn.executemany, sql, parameters)
        return Cursor(self, cursor)

    @contextmanager
    async def executescript(self, sql_script: str) -> Cursor:
        """Helper to create a cursor and execute a user script."""
        cursor = await self._execute(self._conn.executescript, sql_script)
        return Cursor(self, cursor)

    async def interrupt(self) -> None:
        """Interrupt pending queries."""
        return self._conn.interrupt()

    async def create_function(
        self, name: str, num_params: int, func: Callable, deterministic: bool = False
    ) -> None:
        """
        Create user-defined function that can be later used
        within SQL statements. Must be run within the same thread
        that query executions take place so instead of executing directly
        against the connection, we defer this to `run` function.

        If ``deterministic`` is true, the created function is marked as deterministic,
        which allows SQLite to perform additional optimizations. This flag is supported
        by SQLite 3.8.3 or higher, ``NotSupportedError`` will be raised if used with
        older versions.
        """
        assert False
        await self._execute(
            self._conn.create_function,
            name,
            num_params,
            func,
            deterministic=deterministic,
        )

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    @property
    def isolation_level(self) -> Optional[str]:
        return self._conn.isolation_level

    @isolation_level.setter
    def isolation_level(self, value: IsolationLevel) -> None:
        self._conn.isolation_level = value

    @property
    def row_factory(self) -> Optional[Type]:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, factory: Optional[Type]) -> None:
        self._conn.row_factory = factory

    @property
    def text_factory(self) -> Callable[[bytes], Any]:
        return self._conn.text_factory

    @text_factory.setter
    def text_factory(self, factory: Callable[[bytes], Any]) -> None:
        self._conn.text_factory = factory

    @property
    def total_changes(self) -> int:
        return self._conn.total_changes

    async def enable_load_extension(self, value: bool) -> None:
        await self._execute(self._conn.enable_load_extension, value)  # type: ignore

    async def load_extension(self, path: str):
        await self._execute(self._conn.load_extension, path)  # type: ignore

    async def set_progress_handler(
        self, handler: Callable[[], Optional[int]], n: int
    ) -> None:
        await self._execute(self._conn.set_progress_handler, handler, n)

    async def set_trace_callback(self, handler: Callable) -> None:
        await self._execute(self._conn.set_trace_callback, handler)

    async def iterdump(self) -> AsyncIterator[str]:
        """
        Return an async iterator to dump the database in SQL text format.

        Example::

            async for line in db.iterdump():
                ...

        """
        dump_queue: Queue = Queue()

        def dumper():
            try:
                for line in self._conn.iterdump():
                    dump_queue.put_nowait(line)
                dump_queue.put_nowait(None)

            except Exception:
                LOG.exception("exception while dumping db")
                dump_queue.put_nowait(None)
                raise

        fut = self._execute(dumper)
        task = asyncio.ensure_future(fut)

        while True:
            try:
                line: Optional[str] = dump_queue.get_nowait()
                if line is None:
                    break
                yield line

            except Empty:
                if task.done():
                    LOG.warning("iterdump completed unexpectedly")
                    break

                await asyncio.sleep(0.01)

        await task

    async def backup(
        self,
        target: Union["Connection", sqlite3.Connection],
        *,
        pages: int = 0,
        progress: Optional[Callable[[int, int, int], None]] = None,
        name: str = "main",
        sleep: float = 0.250,
    ) -> None:
        """
        Make a backup of the current database to the target database.

        Takes either a standard sqlite3 or aiosqlite Connection object as the target.
        """
        if isinstance(target, Connection):
            target = target._conn

        await self._execute(
            self._conn.backup,
            target,
            pages=pages,
            progress=progress,
            name=name,
            sleep=sleep,
        )


# # Should this be a thread like aiosqlite:core:Connection

# class AsyncRPCConnection:
#     """
#     Proxy for Sqlite connection object..
#     provides an AsyncRPCCurson object
#     """

#     def __init__(
#         self,
#         connector: Callable[[], sqlite3.Connection],
#         iter_chunk_size: int = None, # iter_chunk_size shouldn't be None but whatever
#         loop: Optional[asyncio.AbstractEventLoop] = None,
#             timeout  = None # sqlite connector doesn't need timeout -- but rpc connector does 
#     ) -> None:
#         super().__init__()
#         self._running = True
#         self._connection: Optional[sqlite3.Connection] = None
#         self._connector = connector
#         self._tx: SimpleQueue[Tuple[asyncio.Future, Callable[[], Any]]] = SimpleQueue()
#         self._iter_chunk_size = iter_chunk_size

#         if loop is not None:
#             warn(
#                 "aiosqlite.Connection no longer uses the `loop` parameter",
#                 DeprecationWarning,
#             )

#     def _stop_running(self):
#         self._running = False
#         # PEP 661 is not accepted yet, so we cannot type a sentinel
#         self._tx.put_nowait(_STOP_RUNNING_SENTINEL)  # type: ignore[arg-type]

        
#     # def __init__(self, **kwargs):
#     #     pass

#     def create_function(self, *args, **kwargs):
#         pass

#     def rollback(self, *args, **kwargs):
#         pass

#     async def _execute(self, fn, *args, **kwargs):
#         """Queue a function with the given arguments for execution."""
#         if not self._running or not self._connection:
#             raise ValueError("Connection closed")

#         function = partial(fn, *args, **kwargs)
#         future = asyncio.get_event_loop().create_future()

#         self._tx.put_nowait((future, function))

#         return await future
    
#     @contextmanager
#     async def cursor(self, *args, **kwargs):
#         return Cursor(self, await self._execute(self._conn.cursor))
#         pass
import sys
import traceback
class RemoteSqliteCursor:
    def execute(self, sql: str, parameters: tuple = ...) -> "RemoteSqliteCursor":
        """
        Execute a SQL statement with optional parameters.

        Args:
            sql (str): The SQL query string.
            parameters (tuple, optional): Parameters to substitute into the SQL statement.

        Returns:
            RemoteSqliteCursor: The cursor instance.
        """

        print(f"The sql is {sql}")
        #traceback.print_stack(file=sys.stdout)
        # I am not sure what the RemoteSqliteCursor.execute should return
        # chatgpt says it returns a cursor object.
        # debug says This result object does not return rows. It has been closed automatically
        # so i assuming some sort of result object only. 
        return []
        pass

    def executemany(self, sql: str, seq_of_parameters: list) -> "RemoteSqliteCursor":
        """
        Execute a SQL statement against all parameter sequences or mappings in a list.

        Args:
            sql (str): The SQL query string.
            seq_of_parameters (list): A list of parameter tuples to execute.

        Returns:
            RemoteSqliteCursor: The cursor instance.
        """
        assert False
        pass

    def executescript(self, script: str) -> "RemoteSqliteCursor":
        """
        Execute a multiple SQL commands script.

        Args:
            script (str): The SQL script.

        Returns:
            RemoteSqliteCursor: The cursor instance.
        """
        pass

    def fetchone(self) -> tuple:
        """
        Fetch the next row of a query result set.

        Returns:
            tuple: A single result row or None if no more rows are available.
        """
        pass

    def fetchmany(self, size: int = ...) -> list:
        """
        Fetch the next set of rows of a query result, limited by `size`.

        Args:
            size (int, optional): The number of rows to fetch. Defaults to the cursor's arraysize.

        Returns:
            list: A list of result rows.
        """
        pass

    def fetchall(self) -> list:
        """
        Fetch all remaining rows of a query result.

        Returns:
            list: A list of all remaining rows in the result set.
        """
        # just connect remotely and fetch all the rows.
        assert False
        
        pass

    def close(self) -> None:
        """
        Close the cursor. Further operations will raise an exception.
        """
        pass

    def setinputsizes(self, sizes: list) -> None:
        """
        Set input sizes for parameters. Typically unused in SQLite.

        Args:
            sizes (list): A list of input sizes.
        """
        pass

    def setoutputsize(self, size: int, column: int = ...) -> None:
        """
        Set output size. Typically unused in SQLite.

        Args:
            size (int): The output size.
            column (int, optional): The column index for which to set the output size.
        """
        pass

    @property
    def rowcount(self) -> int:
        """
        Get the number of rows that the last `execute` produced or affected.

        Returns:
            int: The number of rows.
        """
        pass

    @property
    def description(self) -> list:
        """
        Get information about the result columns of the last `execute` statement.

        Returns:
            list: A list of 7-item tuples describing each result column.
        """
        #TODO: this is important
        #         The description property of a cursor object in SQLite (or similar database interfaces in Python) provides metadata about the result set of the last executed query. Specifically, it returns a list of 7-item tuples, each of which describes a column in the result set.

        # Here’s what each item in the tuple represents:

        #     Column Name (str): The name of the column in the result set.
        #     Type Code: The type of the column's data, often represented as a type indicator or None if not provided.
        #     Display Size: (Optional) The size of the column's data when displayed.
        #     Internal Size: The size of the column's data in bytes.
        #     Precision: (Optional) The precision of numeric data types.
        #     Scale: (Optional) The scale of numeric data types.
        #     Nullability: A flag indicating if the column can store NULL values.
        
        #In the case of SQLite, only the column name is guaranteed to be populated, and the other fields are typically None.
        return [
            ("id", None, None, None, None, None, None),
            ("name", None, None, None, None, None, None)
        ]

        pass

    @property
    def lastrowid(self) -> int:
        """
        Get the row ID of the last inserted row.

        Returns:
            int: The row ID.
        """
        pass

    @property
    def arraysize(self) -> int:
        """
        Get or set the number of rows to fetch at a time with `fetchmany`.

        Returns:
            int: The array size.
        """
        pass

    @arraysize.setter
    def arraysize(self, size: int) -> None:
        """
        Set the number of rows to fetch at a time with `fetchmany`.

        Args:
            size (int): The number of rows to fetch at a time.
        """
        pass

    
class RemoteSqliteConnection:
    """
    Proxy for Sqlite connection object..
    provides an AsyncRPCCurson object
    """
    def __init__(self, loc, **kwargs):
        # Create an internal sqlite3.Connection instance
        #self._conn = sqlite3.connect(loc, **kwargs)
    
        pass

    async def connect(self):
        await asyncio.sleep(1)
        #TODO: host should come from connection string
        #port: 8888
        
        host = "localhost"
        port = 8888

        # TODO: find a proper way to connect
        # probably needs a change in 
        self.rsclient = aiorpcx.connect_rs(host, port)
        self._transport, self.protocol = await self.rsclient.create_connection()
        self.session = self.protocol.session

    # Wrap `cursor` method
    def cursor(self):
        #return self._conn.cursor()
        return RemoteSqliteCursor()
        pass

    # Wrap `commit` method
    def commit(self):
        #return self._conn.commit()
        pass

    # Wrap `rollback` method
    def rollback(self):
        #return self._conn.rollback()
        pass
    
    # Wrap `close` method
    async def close(self):
        #return self._conn.close()
        await self.session.close()
        assert False
        pass
    
    # Wrap `execute` method
    def execute(self, *args, **kwargs):
        #return self._conn.execute(*args, **kwargs)
        pass

    # Wrap `executemany` method
    def executemany(self, *args, **kwargs):
        #return self._conn.executemany(*args, **kwargs)
        pass
    

    # Wrap `executescript` method
    def executescript(self, *args, **kwargs):
        #return self._conn.executescript(*args, **kwargs)
        pass

    # Wrap `create_function` method
    def create_function(self, *args, **kwargs):
        #return self._conn.create_function(*args, **kwargs)
        pass

    # Wrap `create_aggregate` method
    def create_aggregate(self, *args, **kwargs):
        #return self._conn.create_aggregate(*args, **kwargs)
        pass

    # Wrap `set_trace_callback` method
    def set_trace_callback(self, *args, **kwargs):
        #return self._conn.set_trace_callback(*args, **kwargs)
        pass

    # Wrap `row_factory` property
    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, factory):
        self._conn.row_factory = factory

    # Add context manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        

    
def connect(
    database: Union[str, Path],
    *,
    iter_chunk_size=64,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    **kwargs: Any,
) -> Connection:
    """Create and return a connection proxy to the sqlite database."""

    if loop is not None:
        warn(
            "aiosqlite.connect() no longer uses the `loop` parameter",
            DeprecationWarning,
        )

    async def connector() -> sqlite3.Connection:
        if isinstance(database, str):
            loc = database
        elif isinstance(database, bytes):
            loc = database.decode("utf-8")
        else:
            loc = str(database)

        # This isn't AsyncRPCConnection but simple
        # RemoteSqliteConnection
        rpc_connection_handle = RemoteSqliteConnection(loc, **kwargs) #AsyncRPCConnection(loc, **kwargs)
        await rpc_connection_handle.connect()
        return rpc_connection_handle #sqlite3.connect(loc, **kwargs)

    # don't know why we need Connection and connector
    # but lets play along
    return Connection(connector, iter_chunk_size)
