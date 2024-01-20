import asyncpg
from typing import List
from uuid import UUID
from tipg.settings import PostgresSettings
from tipg.collections import Column, Collection, Catalog
import json


# TODO: Check if we can reuse the connection of TIPG. At the moment it was considered easier to just open a new connection.
class LayerCatalog:
    def __init__(self, layer_catalog_obj=None):
        self.pool = None
        self.listener_conn = None
        self.layer_catalog_obj = layer_catalog_obj

    async def connect(self):
        """Connect to the database."""
        self.pool = await asyncpg.create_pool(str(PostgresSettings().database_url))

    async def disconnect(self):
        """Disconnect from the database."""
        await self.pool.close()

    async def listen(self):
        """Listen to the layer_changes channel."""
        self.listener_conn = await asyncpg.connect(str(PostgresSettings().database_url))
        await self.listener_conn.add_listener("layer_changes", self.on_layer_changes)

    async def unlisten(self):
        """Unlisten to the layer_changes channel."""
        await self.listener_conn.remove_listener("layer_changes", self.on_layer_changes)
        await self.listener_conn.close()

    async def get(self, layer_id: UUID = None) -> List[dict]:
        """Get all layers when passing now layer_id or get the layer with the given layer_id."""

        # Build and condition
        condition_layer_id = f"AND id = '{layer_id}'" if layer_id else ""

        # Get layers
        async with self.pool.acquire() as conn:
            sql = f"""
                WITH with_bounds AS (
                    SELECT
                        l.*,
                        ST_XMin(e.e) AS xmin,
                        ST_YMin(e.e) AS ymin,
                        ST_XMax(e.e) AS xmax,
                        ST_YMax(e.e) AS ymax
                    FROM customer.layer l, LATERAL ST_Envelope(extent) e
                    WHERE type IN ('feature', 'table')
                    {condition_layer_id}
                )
                SELECT jsonb_build_object('layer_id', id, 'user_id', replace(user_id::text, '-', ''), 'id', replace(id::text, '-', ''), 'name', name, 'bounds', COALESCE(
                        array[xmin, ymin, xmax, ymax],
                        ARRAY[-180, -90, 180, 90]
                    ), 'attribute_mapping', attribute_mapping, 'geom_type', feature_layer_geometry_type)
                FROM with_bounds
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
            columns.append(layer_id_col)

            # Loop through attributes and create column objects
            for k in obj["attribute_mapping"]:
                # Make data_type double precision if float as the Column does not know float as term (only float8).
                data_type = k.split("_")[0] if k.split("_")[0] != "float" else "double precision"
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
                table = obj["geom_type"] + "_" + obj["user_id"]
            else:
                geom_col = None
                table = "no_geometry" + "_" + obj["user_id"]

            # Append ID column
            id_col = Column(name="id", description="id", type="integer")
            columns.append(id_col)

            # Define collection
            collection = Collection(
                type="Table",
                id="user_data." + obj["id"],
                table=table,
                schema="user_data",
                id_column=id_col,
                geometry_column=geom_col,
                table_columns=columns,
                properties=columns,
            )
            # Append collection to collection object
            collections["user_data." + obj["id"]] = collection

        return collections

    async def on_layer_changes(self, connection, pid, channel, payload):
        """Handle layer changes"""

        operation, layer_id = payload.split(":", 1)
        if operation == "UPDATE":
            await self.update_insert(layer_id)
        elif operation == "DELETE":
            await self.delete(layer_id)
        elif operation == "INSERT":
            await self.update_insert(layer_id)

    async def delete(self, layer_id):
        """Remove the corresponding collection for the given ID"""
        collection_key = (
            "user_data." + layer_id
        )  # Assuming the ID corresponds directly to the collection key.
        if collection_key in self.layer_catalog_obj["collections"]:
            del self.layer_catalog_obj["collections"][collection_key]

    async def update_insert(self, layer_id):
        """Update or insert a collection into the catalog"""
        changed_layer = await self.get(layer_id)
        if changed_layer:
            collections = self.build_collection(changed_layer)
            # Insert the new collection into the catalog
            self.layer_catalog_obj["collections"].update(collections)

    async def init(self):
        """Initialize the catalog. It will load all feature layers from the database and build a collection object."""
        layer_objs = await self.get()
        collections = self.build_collection(layer_objs)
        return Catalog(collections=collections)


layer_catalog = LayerCatalog()
