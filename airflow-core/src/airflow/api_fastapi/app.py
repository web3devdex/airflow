# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.routing import Mount

from airflow.api_fastapi.common.dagbag import create_dag_bag
from airflow.api_fastapi.core_api.app import (
    init_config,
    init_error_handlers,
    init_flask_plugins,
    init_middlewares,
    init_ui_plugins,
    init_views,
)
from airflow.api_fastapi.execution_api.app import create_task_execution_api_app
from airflow.configuration import conf
from airflow.exceptions import AirflowConfigException
from airflow.utils.providers_configuration_loader import providers_configuration_loaded

if TYPE_CHECKING:
    from airflow.api_fastapi.auth.managers.base_auth_manager import BaseAuthManager

API_BASE_URL = conf.get("api", "base_url", fallback="")
if not API_BASE_URL or not API_BASE_URL.endswith("/"):
    API_BASE_URL += "/"
API_ROOT_PATH = urlsplit(API_BASE_URL).path

# Define the full path on which the potential auth manager fastapi is mounted
AUTH_MANAGER_FASTAPI_APP_PREFIX = f"{API_ROOT_PATH}auth"

log = logging.getLogger(__name__)

app: FastAPI | None = None
auth_manager: BaseAuthManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for route in app.routes:
            if isinstance(route, Mount) and isinstance(route.app, FastAPI):
                await stack.enter_async_context(
                    route.app.router.lifespan_context(route.app),
                )
        app.state.lifespan_called = True
        yield


@providers_configuration_loaded
def create_app(apps: str = "all") -> FastAPI:
    apps_list = apps.split(",") if apps else ["all"]

    app = FastAPI(
        title="Airflow API",
        description="Airflow API. All endpoints located under ``/api/v2`` can be used safely, are stable and backward compatible. "
        "Endpoints located under ``/ui`` are dedicated to the UI and are subject to breaking change "
        "depending on the need of the frontend. Users should not rely on those but use the public ones instead.",
        lifespan=lifespan,
        root_path=API_ROOT_PATH.removesuffix("/"),
        version="2",
    )

    dag_bag = create_dag_bag()

    if "execution" in apps_list or "all" in apps_list:
        task_exec_api_app = create_task_execution_api_app()
        task_exec_api_app.state.dag_bag = dag_bag
        init_error_handlers(task_exec_api_app)
        app.mount("/execution", task_exec_api_app)

    if "core" in apps_list or "all" in apps_list:
        app.state.dag_bag = dag_bag
        init_plugins(app)
        init_auth_manager(app)
        init_flask_plugins(app)
        init_ui_plugins(app)
        init_views(app)  # Core views need to be the last routes added - it has a catch all route
        init_error_handlers(app)
        init_middlewares(app)

    init_config(app)

    return app


def cached_app(config=None, testing=False, apps="all") -> FastAPI:
    """Return cached instance of Airflow API app."""
    global app
    if not app:
        app = create_app(apps=apps)
    return app


def purge_cached_app() -> None:
    """Remove the cached version of the app and auth_manager in global state."""
    global app, auth_manager
    app = None
    auth_manager = None


def get_auth_manager_cls() -> type[BaseAuthManager]:
    """
    Return just the auth manager class without initializing it.

    Useful to save execution time if only static methods need to be called.
    """
    auth_manager_cls = conf.getimport(section="core", key="auth_manager")

    if not auth_manager_cls:
        raise AirflowConfigException(
            "No auth manager defined in the config. Please specify one using section/key [core/auth_manager]."
        )

    return auth_manager_cls


def create_auth_manager() -> BaseAuthManager:
    """Create the auth manager."""
    global auth_manager
    auth_manager_cls = get_auth_manager_cls()
    auth_manager = auth_manager_cls()
    return auth_manager


def init_auth_manager(app: FastAPI | None = None) -> BaseAuthManager:
    """Initialize the auth manager."""
    am = create_auth_manager()
    am.init()
    if app:
        app.state.auth_manager = am

    if app and (auth_manager_fastapi_app := am.get_fastapi_app()):
        app.mount("/auth", auth_manager_fastapi_app)

    return am


def get_auth_manager() -> BaseAuthManager:
    """Return the auth manager, provided it's been initialized before."""
    global auth_manager

    if auth_manager is None:
        raise RuntimeError(
            "Auth Manager has not been initialized yet. "
            "The `init_auth_manager` method needs to be called first."
        )
    return auth_manager


def init_plugins(app: FastAPI) -> None:
    """Integrate FastAPI app, middlewares and UI plugins."""
    from airflow import plugins_manager

    plugins_manager.initialize_fastapi_plugins()

    # After calling initialize_fastapi_plugins, fastapi_apps cannot be None anymore.
    for subapp_dict in cast("list", plugins_manager.fastapi_apps):
        name = subapp_dict.get("name")
        subapp = subapp_dict.get("app")
        if subapp is None:
            log.error("'app' key is missing for the fastapi app: %s", name)
            continue
        url_prefix = subapp_dict.get("url_prefix")
        if url_prefix is None:
            log.error("'url_prefix' key is missing for the fastapi app: %s", name)
            continue

        log.debug("Adding subapplication %s under prefix %s", name, url_prefix)
        app.mount(url_prefix, subapp)

    # After calling initialize_fastapi_plugins, fastapi_root_middlewares cannot be None anymore.
    for middleware_dict in cast("list", plugins_manager.fastapi_root_middlewares):
        name = middleware_dict.get("name")
        middleware = middleware_dict.get("middleware")
        args = middleware_dict.get("args", [])
        kwargs = middleware_dict.get("kwargs", {})

        if middleware is None:
            log.error("'middleware' key is missing for the fastapi middleware: %s", name)
            continue

        log.debug("Adding root middleware %s", name)
        app.add_middleware(middleware, *args, **kwargs)
