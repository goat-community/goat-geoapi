import asyncio
import json
from typing import List
from uuid import UUID

import asyncpg
from fastapi import FastAPI
from tipg.collections import Catalog, Collection, Column
from tipg.settings import PostgresSettings

class Collection(Collection):
    distributed: bool = False

# TODO: Check if we can reuse the connection of TIPG. At the moment it was considered easier to just open a new connection.
class LayerCatalog:
    def __init__(self, app: FastAPI = None):
        self.listener_task = None
        self.app = app

    @staticmethod
    async def asyncpg_listen(
        channel,
        notification_handler,
        reconnect_handler=None,
        *,
        conn_check_interval=60,
        conn_check_timeout=5,
        reconnect_delay=0,
    ):
        """Listen to a PostgreSQL channel using asyncpg."""
        while True:
            try:
                conn = await asyncpg.connect(str(PostgresSettings().database_url))
                await conn.add_listener(channel, notification_handler)

                if reconnect_handler is not None:
                    await reconnect_handler(conn)

                while True:
                    await asyncio.sleep(conn_check_interval)
                    await conn.execute("select 1", timeout=conn_check_timeout)

            except asyncio.CancelledError:
                print("Listener task was cancelled")
                if conn:
                    print("Closing connection")
                    await conn.close()
                raise

            except:  # noqa: E722
                # Probably lost connection
                pass

            if reconnect_delay > 0:
                await asyncio.sleep(reconnect_delay)

    async def start(self):
        """Listen to the layer_changes channel."""
        print("Starting catalog listener.")
        self.listener_task = asyncio.create_task(
            self.asyncpg_listen(
                "layer_changes", self.listener_handler, self.listener_reconnect_handler
            )
        )

    async def listener_handler(self, conn, pid, channel, payload):
        """Handle layer changes"""
        operation, layer_id = payload.split(":", 1)
        print(
            f"Received notification on channel {channel}: {operation} on layer {layer_id}"
        )
        if operation == "UPDATE":
            await self.update_insert(layer_id, conn)
        elif operation == "DELETE":
            await self.delete(layer_id)
        elif operation == "INSERT":
            await self.update_insert(layer_id, conn)

    async def listener_reconnect_handler(self, conn):
        """Reconnect handler"""
        print("Reading catalog data")
        self.app.state.collection_catalog = await self.read_catalog(conn)

    async def stop(self):
        """Unlisten to the layer_changes channel."""
        self.listener_task.cancel()

    async def get(self, layer_id: UUID = None, conn=None) -> List[dict]:
        """Get all layers when passing now layer_id or get the layer with the given layer_id."""

        # Build and condition
        condition_layer_id = f"AND id = '{layer_id}'" if layer_id else ""

        # Get layers
        sql = f"""
                WITH with_bounds AS (
                SELECT
                    l.*,
                    ST_XMin(e.e) AS xmin,
                    ST_YMin(e.e) AS ymin,
                    ST_XMax(e.e) AS xmax,
                    ST_YMax(e.e) AS ymax,
                    CASE WHEN feature_layer_geometry_type IS NOT NULL AND feature_layer_type = 'street_network'
                    THEN feature_layer_type || '_' || feature_layer_geometry_type || '_' || replace(user_id::text, '-', '')
                    WHEN feature_layer_geometry_type IS NOT NULL
                    THEN feature_layer_geometry_type || '_' || replace(user_id::text, '-', '')
                    ELSE replace(user_id::text, '-', '')
                    END AS table_name
                FROM customer.layer l, LATERAL ST_Envelope(extent) e
                WHERE type IN ('feature', 'table')
                {condition_layer_id}
                ),
                checked_distributed AS
                (
                    SELECT w.*, CASE WHEN table_name_distributed IS NULL THEN FALSE ELSE TRUE END AS distributed
                    FROM with_bounds w
                    LEFT JOIN LATERAL 
                    (
                        SELECT pg_dist_partition.logicalrelid::regclass AS table_name_distributed
                        FROM pg_class
                        LEFT JOIN pg_dist_partition
                        ON pg_class.oid = pg_dist_partition.logicalrelid
                        WHERE pg_class.relname = table_name
                        AND pg_class.relnamespace = 'user_data'::regnamespace
                    ) j ON TRUE
                )
                SELECT jsonb_build_object('layer_id', id, 'user_id', replace(user_id::text, '-', ''), 'id', replace(id::text, '-', ''), 'name', name, 
                        'bounds', COALESCE(array[xmin, ymin, xmax, ymax], ARRAY[-180, -90, 180, 90]),
                        'attribute_mapping', attribute_mapping, 'feature_layer_type', feature_layer_type, 'geom_type', feature_layer_geometry_type, 'table_name', table_name, 'distributed', distributed)
                FROM checked_distributed;
            """
        rows = await conn.fetch(sql)
        return [json.loads(dict(row)["jsonb_build_object"]) for row in rows]

    def build_collection(self, layer_objs: List[dict]):
        """Build a collection using collection and column types from tipg from a layer."""

        collections = {}
        for obj in layer_objs:
            columns = []

            # Append layer id column
            layer_id_col = Column(name="layer_id", type="text", description="layer_id")
            h3_3_col = Column(name="h3_3", type="integer", description="h3_3")
            columns.append(layer_id_col)
            columns.append(h3_3_col)

            # Loop through attributes and create column objects
            for k in obj["attribute_mapping"]:
                # Make data_type double precision if float as the Column does not know float as term (only float8).
                data_type = (
                    k.split("_")[0]
                    if k.split("_")[0] != "float"
                    else "double precision"
                )
                column = Column(
                    name=obj["attribute_mapping"][k],
                    type=data_type,
                    description=k,
                )
                columns.append(column)

            # Get geometry column if geom_type is not None
            if obj["geom_type"]:
                geom_col = Column(
                    name="geom",
                    type="geometry",
                    description="geom",
                    geometry_type=obj["geom_type"],
                    srid=4326,
                    bounds=obj["bounds"],
                )
                columns.append(geom_col)
            else:
                geom_col = None

            # Append ID column
            id_col = Column(name="id", description="id", type="uuid")
            columns.append(id_col)

            # Define collection
            collection = Collection(
                type="Table",
                id="user_data." + obj["id"],
                table=obj["table_name"],
                schema="user_data",
                id_column=id_col,
                geometry_column=geom_col,
                table_columns=columns,
                properties=columns,
                distributed=obj["distributed"],
            )
            # Append collection to collection object
            collections["user_data." + obj["id"]] = collection

        return collections

    async def delete(self, layer_id):
        """Remove the corresponding collection for the given ID"""
        collection_key = (
            "user_data." + layer_id
        )  # Assuming the ID corresponds directly to the collection key.
        if collection_key in self.app.state.collection_catalog["collections"]:
            del self.app.state.collection_catalog["collections"][collection_key]

    async def update_insert(self, layer_id, conn):
        """Update or insert a collection into the catalog"""
        changed_layer = await self.get(layer_id, conn)
        if changed_layer:
            collections = self.build_collection(changed_layer)
            # Insert the new collection into the catalog
            self.app.state.collection_catalog["collections"].update(collections)

    async def read_catalog(self, conn):
        """Initialize the catalog. It will load all feature layers from the database and build a collection object."""
        layer_objs = await self.get(conn=conn)
        collections = self.build_collection(layer_objs)
        return Catalog(collections=collections)
