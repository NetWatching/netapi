import inspect
import json
import re
from fastapi.routing import APIRoute
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi_jwt_auth.exceptions import AuthJWTException
from starlette.middleware.cors import CORSMiddleware
from fastapi_jwt_auth import AuthJWT
from decouple import config
from typing import Optional
import datetime
import humanize
import pymongo.errors
from bson import ObjectId

from src.models.node import TreeJson
from src.crypt import Crypt
from src.mongoDBIO import MongoDBIO
from src.models.models import Settings, ServiceLoginOut, ServiceAggregatorLoginOut, ServiceLogin, \
    ServiceAggregatorLogin, AddAggregatorIn, AddAggregatorOut, APIStatus, DeviceByIdIn, GetAllDevicesOut, \
    AggregatorByID, SetConfig, LinkAgDeviceIN, AggregatorDeviceLinkOut, AggregatorsOut, \
    AddDataForDevices, AggregatorVersionIn, AggregatorVersionOut, AggregatorModulesIn, AggregatorModulesOut, \
    DeviceByIdOut, AddDeviceIn, AddDeviceOut, AddCategoryIn, AddCategoryOut, GetAlertByIdOut, AddDataForDeviceOut, \
    GetAlertsByIdIn, GetAllAlertsOut, GetCategoriesOut, FilterOut, DevicesFilterOut, DeviceConfigOut, DeleteConfig, \
    UpdateDevice, UpdateCategory

# Note: Better logging if needed
# logging.config.fileConfig('loggingx.conf', disable_existing_loggers=False)
# app.add_middleware(RouteLoggerMiddleware)

BAD_PARAM = "Bad Parameter"
start_time = datetime.datetime.now()
version = "DEV"

app = FastAPI()

crypt = Crypt()

mongo = MongoDBIO(
    details=f'mongodb://{config("mDBuser")}:{config("mDBpassword")}@{config("mDBurl")}:{config("mDBport")}/{config("mDBdatabase")}?authSource=admin')

mongo.first_start()

origins = [
    "http://localhost:4200",
    "http://palguin.htl-vil.local",
    "0.0.0.0",
    "localhost",
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="NetAPI",
        version="DEV",
        routes=app.routes,
    )
    openapi_schema["info"]["x-logo"] = {
        "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png"
    }

    cookie_security_schemes = {
        "AccessToken": {
            "type": "http",
            "scheme": "bearer",
            "in": "header",
            "name": "Authorization"
        },
        "RefreshToken": {
            "type": "http",
            "scheme": "bearer",
            "in": "header",
            "name": "Authorization"
        }
    }

    if "components" in openapi_schema:
        openapi_schema["components"].update({"securitySchemes": cookie_security_schemes})
    else:
        openapi_schema["components"] = {"securitySchemes": cookie_security_schemes}

    api_router = [route for route in app.routes if isinstance(route, APIRoute)]

    for route in api_router:
        path = getattr(route, "path")
        endpoint = getattr(route, "endpoint")
        methods = [method.lower() for method in getattr(route, "methods")]

        for method in methods:

            if (
                    re.search("jwt_required", inspect.getsource(endpoint)) or
                    re.search("fresh_jwt_required", inspect.getsource(endpoint)) or
                    re.search("jwt_optional", inspect.getsource(endpoint))
            ):
                openapi_schema["paths"][path][method].update({
                    'security': [{"AccessToken": []}]
                })

            if re.search("jwt_refresh_token_required", inspect.getsource(endpoint)):
                openapi_schema["paths"][path][method].update({
                    'security': [{"RefreshToken": []}]
                })

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


@AuthJWT.load_config
def get_config():
    return Settings()


@app.get("/",
         summary="Check service status",
         response_model=APIStatus)
async def root() -> APIStatus:
    time_delta = datetime.datetime.now() - start_time
    output_time = humanize.naturaldelta(time_delta)
    return APIStatus(version=version, uptime=output_time)


# --- AUTHENTICATION--- #

@app.post('/api/login', response_model=ServiceLoginOut, tags=["Authentication"])
async def login(req: ServiceLogin, authorize: AuthJWT = Depends()):
    """
    /login - POST - authenticates frontend User and returns JWT
    """
    try:
        pw = req.password
        uid = req.id
        name = req.name
    except KeyError:
        raise HTTPException(status_code=400, detail=BAD_PARAM)

    if pw != config("pw"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        expires = datetime.timedelta(minutes=10)
        access_token = authorize.create_access_token(
            subject=uid,
            headers={"name": name},
            expires_time=expires
        )
        expires = datetime.timedelta(minutes=20)
        refresh_token = authorize.create_refresh_token(subject=uid, headers={"name": name}, expires_time=expires)
        return ServiceLoginOut(access_token=access_token, refresh_token=refresh_token)


@app.post('/api/refresh', response_model=ServiceLoginOut, tags=["Authentication"])
async def refresh(authorize: AuthJWT = Depends()):
    """
    /refresh - POST - renew expired access token
    """
    authorize.jwt_refresh_token_required()
    name = authorize.get_unverified_jwt_headers()["name"]

    current_user = authorize.get_jwt_subject()
    expires = datetime.timedelta(minutes=10)
    access_token = authorize.create_access_token(
        subject=current_user,
        headers={"name": name},
        expires_time=expires
    )
    expires = datetime.timedelta(minutes=20)
    refresh_token = authorize.create_refresh_token(subject=current_user, headers={"name": name}, expires_time=expires)
    return ServiceLoginOut(access_token=access_token, refresh_token=refresh_token)


@app.post("/api/aggregator-login", response_model=ServiceAggregatorLoginOut, tags=["Authentication"])
async def aggregator_login(request: ServiceAggregatorLogin, authorize: AuthJWT = Depends()):
    """
    /aggregator-login - POST - aggregator login with token
    """
    try:
        token = request.token
    except KeyError:
        raise HTTPException(status_code=400, detail=BAD_PARAM)

    exists = mongo.check_token(token)

    if exists:
        aggregator_id = str(exists.pk)

        expires = datetime.timedelta(minutes=10)
        access_token = authorize.create_access_token(subject=aggregator_id, expires_time=expires)

        expires = datetime.timedelta(hours=1)
        refresh_token = authorize.create_refresh_token(subject=aggregator_id, expires_time=expires)

        return ServiceAggregatorLoginOut(
            aggregator_id=aggregator_id,
            access_token=access_token,
            refresh_token=refresh_token
        )
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/api/aggregator-refresh", response_model=ServiceLoginOut, tags=["Authentication"])
async def aggregator_refresh(authorize: AuthJWT = Depends()):
    """
    /aggregator-refresh - POST - aggregator login with token
    """
    authorize.jwt_refresh_token_required()
    current_user = authorize.get_jwt_subject()

    expires = datetime.timedelta(minutes=10)
    access_token = authorize.create_access_token(subject=current_user, expires_time=expires)

    expires = datetime.timedelta(hours=1)
    refresh_token = authorize.create_refresh_token(subject=current_user, expires_time=expires)

    return ServiceLoginOut(access_token=access_token, refresh_token=refresh_token)


# --- AGGREGATOR --- #
@app.post("/api/aggregator", status_code=201, response_model=AddAggregatorOut, tags=["Aggregator"])
async def add_aggregator(request: AddAggregatorIn, authorize: AuthJWT = Depends()):
    """
    /aggregator - POST - webinterface can add a new token for a new aggregator
    """
    authorize.jwt_required()

    try:
        token = request.token
        identifier = request.identifier
    except KeyError:
        raise HTTPException(status_code=400, detail=BAD_PARAM)

    try:
        mongo.add_aggregator(token, identifier)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=400, detail="Already exists")

    return AddAggregatorOut(detail="Created")


@app.get("/api/aggregator/{id}", response_model=AggregatorByID, tags=["Aggregator"])
async def get_aggregator_by_id(id: str = "", authorize: AuthJWT = Depends()):
    """
    /aggregator/{id} - GET - returns devices belonging to the aggregator
    """
    authorize.jwt_required()

    if id == "":
        raise HTTPException(status_code=400, detail=BAD_PARAM)

    id = ObjectId(id)

    db_result = mongo.get_aggregator_devices(id)
    try:
        version = db_result.version
        if version == None:
            version = ""
    except AttributeError:
        version = ""
    try:
        ip = db_result.ip
    except AttributeError:
        ip = ""
    try:
        devices = db_result.devices
    except AttributeError:
        devices = []

    devs = []
    for d in devices:

        d = d.to_son().to_dict()
        d["id"] = str(d.pop("_id"))
        if "category" in d:
            category = mongo.get_category_by_id(d.pop("category"))
            if category:
                category = category.category
                # This has to be named typed. IDK why but just let it be
                d["type"] = category
        d["timeout"] = 10

        if "static" in d:
            d.pop("static")

        if "live" in d:
            d.pop("live")


        id = ObjectId(str(d["id"]))
        query_result = mongo.get_device_config(id)
        configs = []
        if not False:
            for c in query_result:
                name = c.type.type
                if c.config is None:
                    c.config = []
                c = c.to_son().to_dict()
                id_ = c["_id"]
                c["id"] = str(id_)
                c.pop("_id")
                c["name"] = name
                c.pop("type")
                configs.append(c)
            d["modules"] = configs

        devs.append(d)
    return AggregatorByID(version=version, ip=ip, devices=devs)


@app.post("/api/aggregator/{id}/version", response_model=AggregatorVersionOut, tags=["Aggregator"])
async def get_aggregator_version_by_id(request: AggregatorVersionIn, id: str = "", authorize: AuthJWT = Depends()):
    """
    /aggregator/{id}/version - POST - set version of the aggregator
    """
    authorize.jwt_required()
    if id != "":
        try:
            ver = request.version
        except KeyError:
            raise HTTPException(status_code=400, detail=BAD_PARAM)

        id = ObjectId(id)
        mongo.set_aggregator_version(id, ver)
        return AggregatorVersionOut(detail="Updated")
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.post("/api/aggregator/{id}/modules", response_model=AggregatorModulesOut, tags=["Aggregator"])
async def aggregator_modules(request: AggregatorModulesIn, id: str = "", authorize: AuthJWT = Depends()):
    """
    /aggregator/{id}/modules - POST - aggregator sends all known modules
    """
    authorize.jwt_required()

    if id != "":
        try:
            modules = request.modules
        except KeyError:
            raise HTTPException(status_code=400, detail=BAD_PARAM)

        id = ObjectId(id)
        mongo.insert_aggregator_modules(modules, id)
        return AggregatorModulesOut(detail="Inserted")
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.get("/api/aggregators", response_model=AggregatorsOut, tags=["Aggregator"])
async def get_aggregators(authorize: AuthJWT = Depends()):
    """
    /aggregators - GET - returns all aggregators
    """
    authorize.jwt_required()

    ags = mongo.get_aggregators()

    return AggregatorsOut(aggregators=ags)


@app.post("/api/aggregator/link/device", response_model=AggregatorDeviceLinkOut, tags=["Aggregator"])
async def link_device_to_aggregator(request: LinkAgDeviceIN, authorize: AuthJWT = Depends()):
    """
    /aggregator/link/device - POST - link device and aggregator
    """
    authorize.jwt_required()

    try:
        ag = request.aggregator
        dev = request.device
    except KeyError:
        raise HTTPException(status_code=400, detail=BAD_PARAM)

    db_result = mongo.set_aggregator_device(ag, dev)

    if not db_result or db_result == -1:
        raise HTTPException(status_code=400, detail=BAD_PARAM)

    return AggregatorDeviceLinkOut(detail="Updated")


# --- DEVICES --- #
@app.get("/api/devices/all", response_model=GetAllDevicesOut, tags=["Device"])
async def get_devices_by_category_full(
        category: Optional[str] = None,
        page: Optional[int] = None,
        amount: Optional[int] = None,
        authorize: AuthJWT = Depends()
):
    """
    /devices/all - GET - get all devices in a full version for the frontend (long execution time!!!)
    """

    authorize.jwt_required()

    categories = []
    if category and "_" in category:
        temps = category.split("_")
        for temp in temps:
            categories.append(ObjectId(temp))
    else:
        if category:
            categories.append(ObjectId(category))

    result = mongo.get_device_by_category_full(categories=categories, page=page, amount=amount)
    if result == -1 or result is False:
        raise HTTPException(status_code=400, detail="Error occurred")
    return GetAllDevicesOut(page=page, amount=amount, total=result["total"], devices=result["devices"])


@app.get("/api/devices", response_model=GetAllDevicesOut, tags=["Device"])
async def get_devices_by_category(
        category: Optional[str] = None,
        page: Optional[int] = None,
        amount: Optional[int] = None,
        authorize: AuthJWT = Depends()
):
    """
    /devices - GET - get all devices in a base version for the frontend
    """

    authorize.jwt_required()

    categories = []
    if category and "_" in category:
        temps = category.split("_")
        for temp in temps:
            categories.append(ObjectId(temp))
    else:
        if category:
            categories.append(ObjectId(category))

    result = mongo.get_device_by_category(categories=categories, page=page, amount=amount)
    if result == -1 or result is False:
        raise HTTPException(status_code=400, detail="Error occurred")
    return GetAllDevicesOut(page=page, amount=amount, total=result["total"], devices=result["devices"])


@app.get("/api/devices/{id}", response_model=DeviceByIdOut, tags=["Device"])
async def device_by_id(id: str, authorize: AuthJWT = Depends()):
    """
    /devices/{id} - GET - returns device infos with specified id
    """
    authorize.jwt_required()

    device = mongo.get_device_by_id(id)

    return DeviceByIdOut(device=device)


@app.post("/api/devices/data", response_model=AddDataForDeviceOut, tags=["Device"])
async def devices_data(request: AddDataForDevices, authorize: AuthJWT = Depends()):
    """
    /devices/data - POST - aggregator sends data which is saved in the Database
    """
    authorize.jwt_required()

    success = mongo.add_data_for_devices(devices=request.devices, external_events=request.external_events)
    if (isinstance(success, bool) is True and success is False) or (isinstance(success, int) and success == -1):
        raise HTTPException(status_code=400, detail="Error occurred")
    return AddDataForDeviceOut(detail="success")


@app.get("/api/devices/{id}/alerts", response_model=GetAllAlertsOut, tags=["Device"])
async def get_alerts_by_device(
        id: str,
        min_severity: Optional[int] = None,
        severity: Optional[str] = None,
        page: Optional[int] = None,
        amount: Optional[int] = None,
        authorize: AuthJWT = Depends()
):
    """
    /devices/{id}/alerts - GET - get all alerts by device id
    """
    authorize.jwt_required()

    events = []
    new_sevs = []
    if severity:
        severities = severity.split('_')

        new_sevs = []
        if severities:
            for sev in severities:
                if mongo.__is_int__(sev):
                    new_sevs.append(int(sev))


        current_events = mongo.get_events(page=page,
                                          amount=amount,
                                          min_severity=min_severity,
                                          severities=new_sevs,
                                          device_id=id)
        if isinstance(current_events, bool) is False and current_events is not False:
            events = current_events
    else:
        severity = None


        current_events = mongo.get_events(page=page,
                                          amount=amount,
                                          min_severity=min_severity,
                                          severities=severity,
                                          device_id=id)
        if isinstance(current_events, bool) is False and current_events is not False:
            events = current_events

    total = mongo.get_event_count(device_id=id, severities=new_sevs, min_severity=min_severity)
    #total = len(events)

    if isinstance(events, bool) and events is False:
        raise HTTPException(status_code=400, detail="Error occurred")
    return GetAllAlertsOut(page=page, amount=amount, total=total, alerts=events)


@app.post("/api/devices", response_model=AddDeviceOut, tags=["Device"])
async def add_device(request: AddDeviceIn, authorize: AuthJWT = Depends()):
    """
    /devices - POST - adds a new device to the DB
    """
    authorize.jwt_required()

    if mongo.add_device_web(request.hostname, request.category, request.ip):
        return AddDeviceOut(detail="success")
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.delete("/api/devices/{id}", response_model=AddDeviceOut, tags=["Device"])
async def delete_device(id: str, authorize: AuthJWT = Depends()):
    """
    /devices/{id} - DELETE - deletes a device from the DB
    """
    authorize.jwt_required()

    id = ObjectId(id)
    if mongo.delete_device_web(id):
        return AddDeviceOut(detail="success")
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.get("/api/devices/{id}/config", response_model=DeviceConfigOut, tags=["Device"])
async def get_device_config(id: str = None, authorize: AuthJWT = Depends()):
    """
    /devices/{id}/config - GET - gets the configs of a device
    """
    authorize.jwt_required()

    if id:
        id = ObjectId(str(id))
        query_result = mongo.get_device_config(id)
        configs = []
        if query_result is not False and query_result != -1:
            for c in query_result:
                name = c.type.type
                type = c.type.to_son().to_dict()
                type.pop("_id")
                conf = crypt.decrypt(type["config"], config("cryptokey"))
                type["config"] = conf.replace('"', "'")
                if c.config is None:
                    c.config = []
                c = c.to_son().to_dict()
                id_ = c["_id"]
                c["id"] = str(id_)
                c.pop("_id")
                c["name"] = name
                c["type"] = type
                c.pop("_cls")
                configs.append(c)
        if not query_result or query_result == -1:
            raise HTTPException(status_code=404, detail="No config found")
        return DeviceConfigOut(configs=configs)
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.post("/api/devices/{id}/config", response_model=AddCategoryOut, tags=["Device"])
async def add_device_config(id: str, request: SetConfig, authorize: AuthJWT = Depends()):
    """
    /devices/{id}/config - POST - adds a new device config to the DB
    """
    authorize.jwt_required()

    id = ObjectId(id)
    query_result = mongo.set_device_config(id, request.config)

    if not query_result or query_result == -1:
        raise HTTPException(status_code=400, detail="Failed")
    return AddCategoryOut(detail="success")

# Do not use
# @app.delete("/api/devices/{id}/config/{module}", tags=["Device"], response_model=AddCategoryOut)
# async def remove_module_from_device(id: str, module: str, authorize: AuthJWT = Depends()):
#     """
#     /devices/{id}/config/{module} - DELETE - remove a Module from a Device
#     """
#     authorize.jwt_required()
#
#     if id and module:
#         id = ObjectId(id)
#         result = mongo.delete_device_config(id, module)
#         if result:
#             return AddCategoryOut(detail="success")
#     raise HTTPException(status_code=400, detail=BAD_PARAM)


# --- Category --- #
@app.get("/api/categories", response_model=GetCategoriesOut, tags=["Category"])
async def get_all_categories(authorize: AuthJWT = Depends()):
    """
    /categories - GET - get all available categories
    """
    authorize.jwt_required()

    result = mongo.get_categories()

    return GetCategoriesOut(categories=result)


@app.post("/api/categories", response_model=AddCategoryOut, tags=["Category"])
async def add_categories(request: AddCategoryIn, authorize: AuthJWT = Depends()):
    """
    /categories - POST - add a new Category to the DB
    """
    authorize.jwt_required()

    if mongo.add_category(category=request.category):
        return AddCategoryOut(detail="success")
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.delete("/api/categories/{id}", response_model=AddCategoryOut, tags=["Category"])
async def delete_categories(id: str, authorize: AuthJWT = Depends()):
    """
    /categories/{id} - DELETE - delete a Category from the DB
    """
    authorize.jwt_required()

    if mongo.delete_category(category_id=id):
        return AddCategoryOut(detail="success")
    raise HTTPException(status_code=400, detail=BAD_PARAM)


# --- Alerts --- #
@app.get("/api/alerts", response_model=GetAllAlertsOut, tags=["Alert"])
async def get_all_alerts(
        min_severity: Optional[int] = None,
        severity: Optional[str] = None,
        page: Optional[int] = None,
        amount: Optional[int] = None,
        authorize: AuthJWT = Depends()
):
    """
    /alerts - GET - get all alerts
    """
    authorize.jwt_required()

    events = []
    new_sevs = []
    if severity:
        severities = severity.split('_')

        new_sevs = []
        if severities:
            for sev in severities:
                if mongo.__is_int__(sev):
                    new_sevs.append(int(sev))

        current_events = mongo.get_events(page=page,
                                          amount=amount,
                                          min_severity=min_severity,
                                          severities=new_sevs)
        if isinstance(current_events, bool) is False and current_events is not False:
            events = current_events
    else:
        severity = None

        current_events = mongo.get_events(page=page,
                                          amount=amount,
                                          min_severity=min_severity,
                                          severities=severity)
        if isinstance(current_events, bool) is False and current_events is not False:
            events = current_events

    total = mongo.get_event_count(severities=new_sevs, min_severity=min_severity)
    #total = len(events)

    if isinstance(events, bool) and events is False:
        raise HTTPException(status_code=400, detail="Error occurred")
    return GetAllAlertsOut(page=page, amount=amount, total=total, alerts=events)


@app.get("/api/alerts/{event_id}", response_model=GetAlertByIdOut, tags=["Alert"])
async def get_alert_by_id(event_id: str, authorize: AuthJWT = Depends()):
    """
    /alerts/{aid} - GET - get specific alert by id
    """
    authorize.jwt_required()

    event = mongo.get_event_by_id(event_id)

    if isinstance(event, dict) is True:
        return GetAlertByIdOut(event=event)
    raise HTTPException(status_code=400, detail=BAD_PARAM)


@app.get("/api/tree", response_model=TreeJson, tags=["Tree View"])
async def get_tree(authorize: AuthJWT = Depends(), vlan_id: Optional[int] = None):
    """
    /tree/ - GET - get tree view
    """
    authorize.jwt_required()
    return mongo.get_tree(vlan_id)


@app.get("/api/devices/filter", response_model=DevicesFilterOut, tags=["Device"])
async def filter_devices(key: str,
                         value: str,
                         page: Optional[int] = None,
                         amount: Optional[int] = None,
                         category_id: Optional[str] = None,
                         authorize: AuthJWT = Depends()
                         ):
    """
    /devices/filter - GET - get filtered devices
    """
    authorize.jwt_required()

    return mongo.filter_devices(key, value, page, amount, category_id)


@app.get("/api/filter", response_model=FilterOut, tags=["Device"])
async def get_filters(authorize: AuthJWT = Depends()):
    """
    /filter - GET - get filter
    """
    authorize.jwt_required()


# --- Modules --- #
@app.get("/api/modules", tags=["Modules"])
async def get_all_modules(authorize: AuthJWT = Depends()):
    """
    /modules - GET - get all modules
    """
    authorize.jwt_required()

    query = mongo.get_types()

    return JSONResponse(status_code=200, content=query)


@app.delete("/api/devices/{device_id}/module/{module_id}", tags=["Modules"], response_model=AddCategoryOut)
async def delete_module_from_device(module_id: str, device_id: str, authorize: AuthJWT = Depends()):
    """
       /modules/{id} - DELETE - delete a specific module from a device
       """
    authorize.jwt_required()

    query_result = mongo.delete_module(module_id=module_id, device_id=device_id)

    if not query_result or query_result == -1:
        raise HTTPException(status_code=400, detail="Not found")
    return AddCategoryOut(detail="success")

@app.put("/api/devices/{id}", tags=["Device"])
async def get_devices_by_category(
        request: UpdateDevice,
        id: str,
        authorize: AuthJWT = Depends()
):
    """
    /devices - PUT - update a device
    """

    authorize.jwt_required()
    hostname = None
    ip = None
    category = None

    if hasattr(request, "hostname"):
        hostname = request.hostname
    if hasattr(request, "ip"):
        ip = request.ip
    if hasattr(request, "category"):
        category = request.category

    if mongo.update_device(id=id, hostname=hostname, ip=ip, category=category):
        return JSONResponse(status_code=200, content="Success")
    raise HTTPException(status_code=400, detail="Failed")


@app.put("/api/categories/{id}", tags=["Category"])
async def get_devices_by_category(
        request: UpdateCategory,
        id: str,
        authorize: AuthJWT = Depends()
):
    """
    /devices - PUT - update a category
    """

    authorize.jwt_required()

    if mongo.update_category(id=id, category=request.category):
        return JSONResponse(status_code=200, content="Success")
    raise HTTPException(status_code=400, detail="Failed")

# --- Exception Handling --- #
@app.exception_handler(AuthJWTException)
def authjwt_exception_handler(request: Request, exc: AuthJWTException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message}
    )
