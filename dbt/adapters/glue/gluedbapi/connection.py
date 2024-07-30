from dataclasses import dataclass
from dbt import exceptions as dbterrors
import boto3
from botocore.config import Config
from waiter import wait
from dbt.adapters.glue.gluedbapi.cursor import GlueCursor, GlueDictCursor
from dbt.adapters.glue.credentials import GlueCredentials
from dbt.adapters.glue.gluedbapi.commons import GlueStatement
import time
import threading
import uuid
from dbt.events import AdapterLogger

logger = AdapterLogger("Glue")


class GlueSessionState:
    READY = "READY"
    FAILED = "FAILED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    CLOSED = "CLOSED"


@dataclass
class GlueConnection:
    _boto3_client_lock = threading.Lock()
    _create_session_config = {}

    def __init__(self, credentials: GlueCredentials, session_id_suffix: str = None, session_config_overrides = {}):
        self.credentials = credentials
        self._session_id_suffix = session_id_suffix
        self._session_config_overrides = session_config_overrides

        self._client = None
        self._session = None
        self._state = None

        self._create_session_config = {}

        for key in self.credentials._connection_keys():
            self._create_session_config[key] = self._session_config_overrides.get(key) or getattr(self.credentials, key)

    def _connect(self):
        logger.debug("GlueConnection connect called")
        if not self.session_id:
            logger.debug("No session present, starting one")
            self._start_session()
        else:
            self._session = {
                "Session": {"Id": self.session_id}
            }
            logger.debug("Existing session with status : " + self.state)
            if self.state == GlueSessionState.CLOSED:
                self._session = self._start_session()

        return self.session_id

    def _start_session(self):
        logger.debug("GlueConnection _start_session called")

        args = {
            "--enable-glue-datacatalog": "true"
        }

        if (self._create_session_config["default_arguments"] is not None):
            args.update(self._string_to_dict(self._create_session_config["default_arguments"].replace(' ', '')))

        if (self._create_session_config["extra_jars"] is not None):
            args["--extra-jars"] = f"{self._create_session_config['extra_jars']}"

        if (self._create_session_config["conf"] is not None):
            args["--conf"] = f"{self._create_session_config['conf']}"

        if (self._create_session_config["extra_py_files"] is not None):
            args["--extra-py-files"] = f"{self._create_session_config['extra_py_files']}"

        additional_args = {}
        additional_args["NumberOfWorkers"] = self._create_session_config["workers"]
        additional_args["WorkerType"] = self._create_session_config["worker_type"]
        additional_args["IdleTimeout"] = self._create_session_config["idle_timeout"]
        additional_args["Timeout"] = self._create_session_config["query_timeout_in_minutes"]
        additional_args["RequestOrigin"] = 'dbt-glue'
        
        if (self._create_session_config['glue_version'] is not None):
            additional_args["GlueVersion"] = f"{self._create_session_config['glue_version']}"
        
        if (self._create_session_config['security_configuration'] is not None):
            additional_args["SecurityConfiguration"] = f"{self._create_session_config['security_configuration']}"
        
        if (self._create_session_config["connections"] is not None):
            additional_args["Connections"] = {"Connections": list(set(self._create_session_config["connections"].split(',')))}

        if (self._create_session_config["tags"] is not None):
            additional_args["Tags"] = self._string_to_dict(self._create_session_config["tags"])

        session_uuid = uuid.uuid4()
        session_uuidStr = str(session_uuid)
        session_prefix = self._create_session_config["role_arn"].partition('/')[2] or self._create_session_config["role_arn"]
        id = f"{session_prefix}-dbt-glue-{session_uuidStr}"

        if self._session_id_suffix:
            id = f"{id}-{self._session_id_suffix}"

        try:
            self._session = self.client.create_session(
                Id=id,
                Role=self._create_session_config["role_arn"],
                DefaultArguments=args,
                Command={
                    "Name": "glueetl",
                    "PythonVersion": "3"
                },
                **additional_args)
        except Exception as e:
            logger.error(
                f"Got an error when attempting to open a GlueSession : {e}"
            )
            raise dbterrors.FailedToConnectError(str(e))
        
        self._session_create_time = time.time()

    def _init_session(self):
        logger.debug("GlueConnection _init_session called")
        logger.debug("GlueConnection session_id : " + self.session_id)
        statement = GlueStatement(client=self.client, session_id=self.session_id, code=SQLPROXY)
        try:
            statement.execute()
        except Exception as e:
            logger.error("Error in GlueCursor execute " + str(e))
            raise dbterrors.ExecutableError(str(e))

        statement = GlueStatement(client=self.client, session_id=self.session_id,
                                  code=f"spark.sql('use {self.credentials.database}')")
        try:
            statement.execute()
        except Exception as e:
            logger.error("Error in GlueCursor execute " + str(e))
            raise dbterrors.ExecutableError(str(e))

    @property
    def session_id(self):
        if not self._session:
            return None
        return self._session.get("Session", {}).get("Id", None)

    @property
    def client(self):
        config = Config(
            retries={
                'max_attempts': 10,
                'mode': 'adaptive'
            }
        )
        if not self._client:
            # refernce on why lock is required - https://stackoverflow.com/a/61943955/6034432
            with self._boto3_client_lock:
                session = boto3.session.Session()
                self._client = session.client("glue", region_name=self.credentials.region, config=config)
        return self._client

    def cancel_statement(self, statement_id):
        logger.debug("GlueConnection cancel_statement called")
        self.client.cancel_statement(
            SessionId=self.session_id,
            Id=statement_id
        )

    def cancel(self):
        logger.debug("GlueConnection cancel called")
        response = self.client.get_statements(SessionId=self.session_id)
        for statement in response["Statements"]:
            if statement["State"] in GlueSessionState.RUNNING:
                self.cancel_statement(statement_id=statement["Id"])

    def close(self):
        if not self.credentials.enable_session_per_model:
            logger.debug("NotImplemented: close")
            return
        logger.debug("GlueConnection close called")
        self.close_session()

    @staticmethod
    def rollback():
        logger.debug("NotImplemented: rollback")

    def cursor(self, as_dict=False) -> GlueCursor:
        logger.debug("GlueConnection cursor called")
        self._connect()
        if self.state == GlueSessionState.READY:
            self._init_session()
            return GlueDictCursor(connection=self) if as_dict else GlueCursor(connection=self)
        else:
            for elapsed in wait(1):
                if self.state == GlueSessionState.READY:
                    self._init_session()
                    return GlueDictCursor(connection=self) if as_dict else GlueCursor(connection=self)
                if ((time.time() - self._session_create_time) if self._session_create_time else elapsed) > self._create_session_config["session_provisioning_timeout_in_seconds"]:
                    raise TimeoutError(f"GlueSession took more than {self._create_session_config['session_provisioning_timeout_in_seconds']} seconds to start")
        

    def close_session(self):
        logger.debug("GlueConnection close_session called")
        if not self._session:
            return

        for elapsed in wait(1):
            if self.state not in [GlueSessionState.PROVISIONING, GlueSessionState.READY, GlueSessionState.RUNNING]:
                return
            
            logger.debug(f"[elapsed {elapsed}s - calling stop_session for {self.session_id} in {self.state} state")
            try:
                self.client.stop_session(Id=self.session_id)
            except Exception as e:
                if "Session is in PROVISIONING status" in str(e):
                    logger.debug(f"session is not yet initialised - retrying to close")
                else:
                    raise e


    @property
    def state(self):
        if self._state in [GlueSessionState.FAILED]:
            return self._state
        try:
            response = self.client.get_session(Id=self.session_id)
            session = response.get("Session", {})
            self._state = session.get("Status")
        except:
            self._state = GlueSessionState.CLOSED
        return self._state

    def _string_to_dict(self, value_to_convert):
        value_in_dictionary = {}
        for i in value_to_convert.split(","):
            value_in_dictionary[i.split("=")[0].strip('\'').replace("\"", "")] = i.split("=")[1].strip('"\'')
        return value_in_dictionary


SQLPROXY = """
import json
import base64
class SqlWrapper2:
    i = 0
    dfs = {}
    @classmethod
    def execute(cls,sql,output=True):
        if "dbt_next_query" in sql:
                response=None
                queries = sql.split("dbt_next_query")
                for q in queries:
                    if (len(q)):
                        if q==queries[-1]:
                            response=cls.execute(q,output=True)
                        else:
                            cls.execute(q,output=False)
                return  response   
                
        spark.conf.set("spark.sql.crossJoin.enabled", "true")    
        df = spark.sql(sql)
        if len(df.schema.fields) == 0:
            dumped_empty_result = json.dumps({"type" : "results","sql" : sql,"schema": None,"results": None})
            if output:
                print (dumped_empty_result)
            else:
                return dumped_empty_result
        results = []
        rowcount = df.count()
        for record in df.rdd.collect():
            d = {}
            for f in df.schema:
                d[f.name] = record[f.name]
            results.append(({"type": "record", "data": d}))
        dumped_results = json.dumps({"type": "results", "rowcount": rowcount,"results": results,"description": [{"name":f.name, "type":str(f.dataType)} for f in df.schema]},default=str)
        if output:
            print(dumped_results)
        else:
            return dumped_results
"""