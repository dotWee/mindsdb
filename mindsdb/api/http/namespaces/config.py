import copy
import shutil
import tempfile
from pathlib import Path
from http import HTTPStatus

from flask import request
from flask_restx import Resource
from flask import current_app as ca

from mindsdb.api.http.namespaces.configs.config import ns_conf
from mindsdb.api.http.utils import http_error
from mindsdb.metrics.metrics import api_endpoint_metrics
from mindsdb.utilities import log
from mindsdb.utilities.functions import decrypt, encrypt
from mindsdb.utilities.config import Config
from mindsdb.integrations.libs.response import HandlerStatusResponse


logger = log.getLogger(__name__)


@ns_conf.route("/")
@ns_conf.param("name", "Get config")
class GetConfig(Resource):
    @ns_conf.doc("get_config")
    @api_endpoint_metrics("GET", "/config")
    def get(self):
        config = Config()
        resp = {"auth": {"http_auth_enabled": config["auth"]["http_auth_enabled"]}}
        for key in ["default_llm", "default_embedding_model", "default_reranking_model"]:
            value = config.get(key)
            if value is not None:
                resp[key] = value
        if "a2a" in config["api"]:
            resp["a2a"] = config["api"]["a2a"]
        return resp

    @ns_conf.doc("put_config")
    @api_endpoint_metrics("PUT", "/config")
    def put(self):
        data = request.json

        allowed_arguments = {"auth", "default_llm", "default_embedding_model", "default_reranking_model"}
        unknown_arguments = list(set(data.keys()) - allowed_arguments)
        if len(unknown_arguments) > 0:
            return http_error(HTTPStatus.BAD_REQUEST, "Wrong arguments", f"Unknown argumens: {unknown_arguments}")

        nested_keys_to_validate = {"auth"}
        for key in data.keys():
            if key in nested_keys_to_validate:
                unknown_arguments = list(set(data[key].keys()) - set(Config()[key].keys()))
                if len(unknown_arguments) > 0:
                    return http_error(
                        HTTPStatus.BAD_REQUEST, "Wrong arguments", f"Unknown argumens: {unknown_arguments}"
                    )

        overwrite_arguments = {"default_llm", "default_embedding_model", "default_reranking_model"}
        overwrite_data = {k: data[k] for k in overwrite_arguments if k in data}
        merge_data = {k: data[k] for k in data if k not in overwrite_arguments}

        if len(overwrite_data) > 0:
            Config().update(overwrite_data, overwrite=True)
        if len(merge_data) > 0:
            Config().update(merge_data)

        Config().update(data)

        return "", 200


@ns_conf.route("/integrations")
@ns_conf.param("name", "List all database integration")
class ListIntegration(Resource):
    @api_endpoint_metrics("GET", "/config/integrations")
    def get(self):
        return {"integrations": [k for k in ca.integration_controller.get_all(show_secrets=False)]}


@ns_conf.route("/all_integrations")
@ns_conf.param("name", "List all database integration")
class AllIntegration(Resource):
    @ns_conf.doc("get_all_integrations")
    @api_endpoint_metrics("GET", "/config/all_integrations")
    def get(self):
        integrations = ca.integration_controller.get_all(show_secrets=False)
        return integrations


@ns_conf.route("/integrations/<name>")
@ns_conf.param("name", "Database integration")
class Integration(Resource):
    @ns_conf.doc("get_integration")
    @api_endpoint_metrics("GET", "/config/integrations/integration")
    def get(self, name):
        integration = ca.integration_controller.get(name, show_secrets=False)
        if integration is None:
            return http_error(HTTPStatus.NOT_FOUND, "Not found", f"Can't find integration: {name}")
        integration = copy.deepcopy(integration)
        return integration

    @ns_conf.doc("put_integration")
    @api_endpoint_metrics("PUT", "/config/integrations/integration")
    def put(self, name):
        params = {}
        if request.is_json:
            params.update((request.json or {}).get("params", {}))
        else:
            params.update(request.form or {})

        if len(params) == 0:
            return http_error(HTTPStatus.BAD_REQUEST, "Wrong argument", "type of 'params' must be dict")

        files = request.files
        temp_dir = None
        if files is not None and len(files) > 0:
            temp_dir = tempfile.mkdtemp(prefix="integration_files_")
            for key, file in files.items():
                temp_dir_path = Path(temp_dir)
                file_name = Path(file.filename)
                file_path = temp_dir_path.joinpath(file_name).resolve()
                if temp_dir_path not in file_path.parents:
                    raise Exception(f"Can not save file at path: {file_path}")
                file.save(file_path)
                params[key] = str(file_path)

        is_test = params.get("test", False)
        # TODO: Move this to new Endpoint

        config = Config()
        secret_key = config.get("secret_key", "dummy-key")

        if is_test:
            del params["test"]
            handler_type = params.pop("type", None)
            params.pop("publish", None)
            try:
                handler = ca.integration_controller.create_tmp_handler(name, handler_type, params)
                status = handler.check_connection()
            except ImportError as e:
                status = HandlerStatusResponse(success=False, error_message=str(e))
            if temp_dir is not None:
                shutil.rmtree(temp_dir)

            resp = status.to_json()

            if status.success and "code" in params:
                if hasattr(handler, "handler_storage"):
                    # attach storage if exists
                    export = handler.handler_storage.export_files()
                    if export:
                        # encrypt with flask secret key
                        encrypted = encrypt(export, secret_key)
                        resp["storage"] = encrypted.decode()

            return resp, 200

        config = Config()
        secret_key = config.get("secret_key", "dummy-key")

        integration = ca.integration_controller.get(name, show_secrets=False)
        if integration is not None:
            return http_error(
                HTTPStatus.BAD_REQUEST, "Wrong argument", f"Integration with name '{name}' already exists"
            )

        try:
            engine = params["type"]
            if engine is not None:
                del params["type"]
            params.pop("publish", False)
            storage = params.pop("storage", None)
            ca.integration_controller.add(name, engine, params)

            # copy storage
            if storage is not None:
                handler = ca.integration_controller.get_data_handler(name)

                export = decrypt(storage.encode(), secret_key)
                handler.handler_storage.import_files(export)

        except Exception as e:
            logger.error(str(e))
            if temp_dir is not None:
                shutil.rmtree(temp_dir)
            return http_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Error", f"Error during config update: {str(e)}")

        if temp_dir is not None:
            shutil.rmtree(temp_dir)
        return {}, 200

    @ns_conf.doc("delete_integration")
    @api_endpoint_metrics("DELETE", "/config/integrations/integration")
    def delete(self, name):
        integration = ca.integration_controller.get(name)
        if integration is None:
            return http_error(
                HTTPStatus.BAD_REQUEST, "Integration does not exists", f"Nothing to delete. '{name}' not exists."
            )
        try:
            ca.integration_controller.delete(name)
        except Exception as e:
            logger.error(str(e))
            return http_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Error", f"Error during integration delete: {str(e)}")
        return "", 200

    @ns_conf.doc("modify_integration")
    @api_endpoint_metrics("POST", "/config/integrations/integration")
    def post(self, name):
        params = {}
        params.update((request.json or {}).get("params", {}))
        params.update(request.form or {})

        if not isinstance(params, dict):
            return http_error(HTTPStatus.BAD_REQUEST, "Wrong argument", "type of 'params' must be dict")
        integration = ca.integration_controller.get(name)
        if integration is None:
            return http_error(
                HTTPStatus.BAD_REQUEST, "Integration does not exists", f"Nothin to modify. '{name}' not exists."
            )
        try:
            if "enabled" in params:
                params["publish"] = params["enabled"]
                del params["enabled"]
            ca.integration_controller.modify(name, params)

        except Exception as e:
            logger.error(str(e))
            return http_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Error", f"Error during integration modification: {str(e)}"
            )
        return "", 200
