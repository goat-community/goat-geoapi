"""
---------------------------------------------------------------------------------
This code is based on or incorporates material from the project:
https://github.com/developmentseed/tipg

The original code/repository is licensed under MIT License.
---------------------------------------------------------------------------------
"""


from contextlib import asynccontextmanager
from tipg import __version__ as tipg_version
from tipg.collections import Collection
from tipg import dependencies
from src.exts import (
    _from,
    get_mvt_point,
    _select_no_geo,
    get_column,
    filter_query,
    _where,
    get_tile,
    Operator as OperatorPatch,
)

# Monkey patch filter query here because it needs to be patched before used by import down
dependencies.filter_query = filter_query

from tipg.database import close_db_connection, connect_to_db
from tipg.factory import Endpoints
from tipg.middleware import CacheControlMiddleware
from tipg.settings import (
    APISettings,
    CustomSQLSettings,
    DatabaseSettings,
    PostgresSettings,
    MVTSettings,
)
from tipg.filter.filters import Operator
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette_cramjam.middleware import CompressionMiddleware
from src.catalog import LayerCatalog
from fastapi.openapi.utils import get_openapi

mvt_settings = MVTSettings()
mvt_settings.max_features_per_tile = 20000
settings = APISettings()
postgres_settings = PostgresSettings()
db_settings = DatabaseSettings()
custom_sql_settings = CustomSQLSettings()


# Monkey patch the function that need modification
Operator.OPERATORS = OperatorPatch.OPERATORS
Collection._from = _from
Collection.get_mvt_point = get_mvt_point
Collection._where = _where
Collection._select_no_geo = _select_no_geo
Collection.get_column = get_column
Collection.get_tile = get_tile


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI Lifespan."""
    # Create Connection Pool
    await connect_to_db(
        app,
        settings=postgres_settings,
        schemas=db_settings.schemas,
        user_sql_files=custom_sql_settings.sql_files,
    )
    # Create Initial Layer Catalog
    layer_catalog = LayerCatalog()
    await layer_catalog.connect()
    app.state.collection_catalog = await layer_catalog.init()
    await layer_catalog.disconnect()

    # # Listen to the layer_changes channel
    layer_catalog_listen = LayerCatalog(app.state.collection_catalog)
    await layer_catalog_listen.connect()
    await layer_catalog_listen.listen()

    yield

    # # Unlisten to layer_changes channel and close the Connection Pool
    await layer_catalog_listen.unlisten()
    await layer_catalog_listen.disconnect()

    await close_db_connection(app)


# Create FastAPI app
app = FastAPI(
    title=settings.name,
    version=tipg_version,
    openapi_url="/api",
    docs_url="/api.html",
    lifespan=lifespan,
)

# Set all CORS enabled origins
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

# Create Endpoints
ogc_api = Endpoints(
    title=settings.name,
    with_tiles_viewer=settings.add_tiles_viewer,
)
# Remove the list all collections endpoint
ogc_api.router.routes = ogc_api.router.routes[1:]
app.include_router(ogc_api.router)
app.add_middleware(CacheControlMiddleware, cachecontrol=settings.cachecontrol)
app.add_middleware(CompressionMiddleware)


@app.get(
    "/healthz",
    description="Health Check.",
    summary="Health Check.",
    operation_id="healthCheck",
    tags=["Health Check"],
)
def ping():
    """Health check."""
    return {"ping": "pongpong!"}
