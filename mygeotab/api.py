# -*- coding: utf-8 -*-

"""
mygeotab.api
~~~~~~~~~~~~

Public objects and methods wrapping the MyGeotab API.
"""

from __future__ import unicode_literals

import copy
import re
import ssl
import sys

import requests
import six
from requests.adapters import HTTPAdapter
from requests.exceptions import Timeout
from requests.packages import urllib3
from six.moves import UserList
from six.moves.urllib.parse import urlparse

from . import __title__, __version__
from .exceptions import AuthenticationException, MyGeotabException, TimeoutException
from .serializers import json_deserialize, json_serialize

DEFAULT_TIMEOUT = 300


class API(object):
    """A simple and Pythonic wrapper for the MyGeotab API.
    """

    def __init__(
        self, username, password=None, database=None, session_id=None, server="my.geotab.com", timeout=DEFAULT_TIMEOUT
    ):
        """Initialize the MyGeotab API object with credentials.

        :param username: The username used for MyGeotab servers. Usually an email address.
        :type username: str
        :param password: The password associated with the username. Optional if `session_id` is provided.
        :type password: str
        :param database: The database or company name. Optional as this usually gets resolved upon authentication.
        :type database: str
        :param session_id: A session ID, assigned by the server.
        :type session_id: str
        :param server: The server ie. my23.geotab.com. Optional as this usually gets resolved upon authentication.
        :type server: str or None
        :param timeout: The timeout to make the call, in seconds. By default, this is 300 seconds (or 5 minutes).
        :type timeout: float or None
        :raise Exception: Raises an Exception if a username, or one of the session_id or password is not provided.
        """
        if username is None:
            raise Exception("`username` cannot be None")
        if password is None and session_id is None:
            raise Exception("`password` and `session_id` must not both be None")
        self.credentials = Credentials(
            username=username, session_id=session_id, database=database, server=server, password=password
        )
        self.timeout = timeout
        self.__reauthorize_count = 0

    @property
    def _server(self):
        if not self.credentials.server:
            self.credentials.server = "my.geotab.com"
        return self.credentials.server

    @property
    def _is_verify_ssl(self):
        """Whether or not SSL be verified.

        :rtype: bool
        :return: True if the calls are being made locally.
        """
        return not any(s in get_api_url(self._server) for s in ["127.0.0.1", "localhost"])

    def call(self, method, **parameters):
        """Makes a call to the API.

        :param method: The method name.
        :type method: str
        :param parameters: Additional parameters to send (for example, search=dict(id='b123') ).
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        :return: The results from the server.
        :rtype: dict or list
        """
        if method is None:
            raise Exception("A method name must be specified")
        params = process_parameters(parameters)
        if self.credentials and not self.credentials.session_id:
            self.authenticate()
        if "credentials" not in params and self.credentials.session_id:
            params["credentials"] = self.credentials.get_param()

        try:
            result = _query(self._server, method, params, self.timeout, verify_ssl=self._is_verify_ssl)
            if result is not None:
                self.__reauthorize_count = 0
            return result
        except MyGeotabException as exception:
            if exception.name == "InvalidUserException":
                if self.__reauthorize_count == 0 and self.credentials.password:
                    self.__reauthorize_count += 1
                    self.authenticate()
                    return self.call(method, **parameters)
                else:
                    raise AuthenticationException(
                        self.credentials.username, self.credentials.database, self.credentials.server
                    )
            raise

    def multi_call(self, calls):
        """Performs a multi-call to the API.

        :param calls: A list of call 2-tuples with method name and params
                      (for example, ('Get', dict(typeName='Trip')) ).
        :type calls: list((str, dict))
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        :return: The results from the server.
        :rtype: list
        """
        formatted_calls = [dict(method=call[0], params=call[1] if len(call) > 1 else {}) for call in calls]
        return self.call("ExecuteMultiCall", calls=formatted_calls)

    def get(self, type_name, **parameters):
        """Gets entities using the API. Shortcut for using call() with the 'Get' method.

        :param type_name: The type of entity.
        :type type_name: str
        :param parameters: Additional parameters to send.
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        :return: The results from the server.
        :rtype: list
        """
        if parameters:
            results_limit = parameters.get("resultsLimit", None)
            if results_limit is not None:
                del parameters["resultsLimit"]
            if "search" in parameters:
                parameters.update(parameters["search"])
            parameters = dict(search=parameters, resultsLimit=results_limit)
        return EntityList(self.call("Get", type_name=type_name, **parameters), type_name=type_name)

    def add(self, type_name, entity):
        """Adds an entity using the API. Shortcut for using call() with the 'Add' method.

        :param type_name: The type of entity.
        :type type_name: str
        :param entity: The entity to add.
        :type entity: dict
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        :return: The id of the object added.
        :rtype: str
        """
        return self.call("Add", type_name=type_name, entity=entity)

    def set(self, type_name, entity):
        """Sets an entity using the API. Shortcut for using call() with the 'Set' method.

        :param type_name: The type of entity.
        :type type_name: str
        :param entity: The entity to set.
        :type entity: dict
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        """
        return self.call("Set", type_name=type_name, entity=entity)

    def remove(self, type_name, entity):
        """Removes an entity using the API. Shortcut for using call() with the 'Remove' method.

        :param type_name: The type of entity.
        :type type_name: str
        :param entity: The entity to remove.
        :type entity: dict
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        """
        return self.call("Remove", type_name=type_name, entity=entity)

    def authenticate(self, is_global=True):
        """Authenticates against the API server.

        :param is_global: If True, authenticate globally. Local login if False.
        :raise AuthenticationException: Raises if there was an issue with authenticating or logging in.
        :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
        :raise TimeoutException: Raises when the request does not respond after some time.
        :return: A Credentials object with a session ID created by the server.
        :rtype: Credentials
        """
        auth_data = dict(
            database=self.credentials.database, userName=self.credentials.username, password=self.credentials.password
        )
        auth_data["global"] = is_global
        try:
            result = _query(self._server, "Authenticate", auth_data, self.timeout, verify_ssl=self._is_verify_ssl)
            if result:
                new_server = result["path"]
                server = self.credentials.server
                if new_server != "ThisServer":
                    server = new_server
                credentials = result["credentials"]
                self.credentials = Credentials(
                    credentials["userName"], credentials["sessionId"], credentials["database"], server
                )
                return self.credentials
        except MyGeotabException as exception:
            if exception.name == "InvalidUserException":
                raise AuthenticationException(
                    self.credentials.username, self.credentials.database, self.credentials.server
                )
            raise

    @staticmethod
    def from_credentials(credentials):
        """Returns a new API object from an existing Credentials object.

        :param credentials: The existing saved credentials.
        :type credentials: Credentials
        :return: A new API object populated with MyGeotab credentials.
        :rtype: API
        """
        return API(
            username=credentials.username,
            password=credentials.password,
            database=credentials.database,
            session_id=credentials.session_id,
            server=credentials.server,
        )


class EntityList(UserList):
    """The customized result list
    """

    def __init__(self, data, type_name):
        """Gets entities using the API. Shortcut for using call() with the 'Get' method.

        :param data: The list of result data.
        :type data: list
        :param type_name: The type of entity.
        :type type_name: str
        """
        super(EntityList, self).__init__(data)
        self.type_name = type_name

    def _repr_pretty_(self, p, cycle):
        """The pretty printer for IPython
        """
        if cycle:
            p.text("{}(...)".format(self.type_name))
        else:
            with p.group(8, "{}([".format(self.type_name), "])"):
                for idx, item in enumerate(self.data):
                    if idx:
                        p.text(",")
                        p.breakable()
                    p.pretty(item)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return self.__class__(self.data[i], self.type_name)
        else:
            return self.data[i]

    def __getslice__(self, i, j):
        i = max(i, 0)
        j = max(j, 0)
        return self.__class__(self.data[i:j], self.type_name)

    def __add__(self, other):
        if isinstance(other, UserList):
            return self.__class__(self.data + other.data, self.type_name)
        elif isinstance(other, type(self.data)):
            return self.__class__(self.data + other, self.type_name)
        return self.__class__(self.data + list(other), self.type_name)

    def __radd__(self, other):
        if isinstance(other, UserList):
            return self.__class__(other.data + self.data, self.type_name)
        elif isinstance(other, type(self.data)):
            return self.__class__(other + self.data, self.type_name)
        return self.__class__(list(other) + self.data, self.type_name)

    def __mul__(self, n):
        return self.__class__(self.data * n, self.type_name)

    __rmul__ = __mul__

    def __copy__(self):
        inst = self.__class__.__new__(self.__class__, self.type_name)
        inst.__dict__.update(self.__dict__)
        # Create a copy and avoid triggering descriptors
        inst.__dict__["data"] = self.__dict__["data"][:]
        return inst

    def sort_by(self, key, reverse=False):
        """Returns an EntityList, sorted by a provided key.

        :param key: The key to sort the data with.
        :type key: str
        :param reverse: If true, reverse the sort direction.
        :type reverse: bool
        :rtype: EntityList
        """

        def sort_by_key(entity):
            prop = entity[key]
            if isinstance(prop, six.string_types):
                return prop.lower()
            return prop

        return self.__class__(sorted(self.data, key=sort_by_key, reverse=reverse), type_name=self.type_name)

    @property
    def first(self):
        """Gets the first entity in the list, if it exists.

        :rtype: dict
        """
        return self.data[0] if self.data else None

    @property
    def last(self):
        """Gets the last entity in the list, if it exists.

        :rtype: dict
        """
        return self.data[-1] if self.data else None

    @property
    def entity(self):
        """Like `first`, but first asserts that there is only one entity in the results list.

        :rtype: dict
        """
        data_length = len(self.data)
        assert data_length == 1, "Expecting one entity, but {} entities were returned".format(data_length)
        return self.first

    def to_dataframe(self, normalize=False):
        """Transforms the data into a pandas DataFrame

        :param normalize: Whether or not to normalize any nested objects in the results into distinct columns.
        :type normalize: bool
        :rtype: pandas.DataFrame
        """
        try:
            import pandas
        except ImportError:
            raise ImportError("The 'pandas' package could not be imported")
        if normalize:
            from pandas.io.json import json_normalize

            return json_normalize(self.data)
        return pandas.DataFrame.from_dict(self.data)


class Credentials(object):
    """The MyGeotab Credentials object.
    """

    def __init__(self, username, session_id, database, server, password=None):
        """Initialize the Credentials object.

        :param username: The username used for MyGeotab servers. Usually an email address.
        :type username: str
        :param session_id: A session ID, assigned by the server.
        :type session_id: str
        :param database: The database or company name. Optional as this usually gets resolved upon authentication.
        :type database: str or None
        :param server: The server ie. my23.geotab.com. Optional as this usually gets resolved upon authentication.
        :type server: str or None
        :param password: The password associated with the username. Optional if `session_id` is provided.
        :type password: str or None
        """
        self.username = username
        self.session_id = session_id
        self.database = database
        self.server = server
        self.password = password

    def __str__(self):
        return "{0} @ {1}/{2}".format(self.username, self.server, self.database)

    def __repr__(self):
        return "Credentials(username={username}, database={database})".format(
            username=self.username, database=self.database
        )

    def get_param(self):
        """A simple representation of the credentials object for passing into the API.authenticate() server call.

        :return: The simple credentials object for use by API.authenticate().
        :rtype: dict
        """
        return dict(userName=self.username, sessionId=self.session_id, database=self.database)


class GeotabHTTPAdapter(HTTPAdapter):
    """HTTP adapter to force use of TLS 1.2 for HTTPS connections.
    """

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self.poolmanager = urllib3.poolmanager.PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, ssl_version=ssl.PROTOCOL_TLSv1_2, **pool_kwargs
        )


def _query(server, method, parameters, timeout=DEFAULT_TIMEOUT, verify_ssl=True):
    """Formats and performs the query against the API.

    :param server: The MyGeotab server.
    :type server: str
    :param method: The method name.
    :type method: str
    :param parameters: The parameters to send with the query.
    :type parameters: dict
    :param timeout: The timeout to make the call, in seconds. By default, this is 300 seconds (or 5 minutes).
    :type timeout: float
    :param verify_ssl: If True, verify the SSL certificate. It's recommended not to modify this.
    :type verify_ssl: bool
    :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
    :raise TimeoutException: Raises when the request does not respond after some time.
    :raise urllib2.HTTPError: Raises when there is an HTTP status code that indicates failure.
    :return: The JSON-decoded result from the server.
    """
    api_endpoint = get_api_url(server)
    params = dict(id=-1, method=method, params=parameters or {})
    headers = get_headers()
    with requests.Session() as session:
        session.mount("https://", GeotabHTTPAdapter())
        try:
            response = session.post(
                api_endpoint,
                data=json_serialize(params),
                headers=headers,
                allow_redirects=True,
                timeout=timeout,
                verify=verify_ssl,
            )
        except Timeout:
            raise TimeoutException(server)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type")
    if content_type and "application/json" not in content_type.lower():
        return response.text
    return _process(json_deserialize(response.text))


def _process(data):
    """Processes the returned JSON from the server.

    :param data: The JSON data in dict form.
    :raise MyGeotabException: Raises when a server exception was encountered.
    :return: The result data.
    """
    if data:
        if "error" in data:
            raise MyGeotabException(data["error"])
        if "result" in data:
            return data["result"]
    return data


def server_call(method, server, timeout=DEFAULT_TIMEOUT, verify_ssl=True, **parameters):
    """Makes a call to an un-authenticated method on a server

    :param method: The method name.
    :type method: str
    :param server: The MyGeotab server.
    :type server: str
    :param timeout: The timeout to make the call, in seconds. By default, this is 300 seconds (or 5 minutes).
    :type timeout: float
    :param verify_ssl: If True, verify the SSL certificate. It's recommended not to modify this.
    :type verify_ssl: bool
    :param parameters: Additional parameters to send (for example, search=dict(id='b123') ).
    :raise MyGeotabException: Raises when an exception occurs on the MyGeotab server.
    :raise TimeoutException: Raises when the request does not respond after some time.
    :return: The result from the server.
    """
    if method is None:
        raise Exception("A method name must be specified")
    if server is None:
        raise Exception("A server (eg. my3.geotab.com) must be specified")
    parameters = process_parameters(parameters)
    return _query(server, method, parameters, timeout=timeout, verify_ssl=verify_ssl)


def process_parameters(parameters):
    """Allows the use of Pythonic-style parameters with underscores instead of camel-case.

    :param parameters: The parameters object.
    :type parameters: dict
    :return: The processed parameters.
    :rtype: dict
    """
    if not parameters:
        return {}
    params = copy.copy(parameters)
    for param_name in parameters:
        value = parameters[param_name]
        server_param_name = re.sub(r"_(\w)", lambda m: m.group(1).upper(), param_name)
        if isinstance(value, dict):
            value = process_parameters(value)
        params[server_param_name] = value
        if server_param_name != param_name:
            del params[param_name]
    return params


def get_api_url(server):
    """Formats the server URL properly in order to query the API.

    :return: A valid MyGeotab API request URL.
    :rtype: str
    """
    parsed = urlparse(server)
    base_url = parsed.netloc if parsed.netloc else parsed.path
    base_url.replace("/", "")
    return "https://" + base_url + "/apiv1"


def get_headers():
    """Gets the request headers.

    :return: The user agent
    :rtype: dict
    """
    return {
        "Content-type": "application/json; charset=UTF-8",
        "User-Agent": "Python/{py_version[0]}.{py_version[1]} {title}/{version}".format(
            py_version=sys.version_info, title=__title__, version=__version__
        ),
    }


__all__ = ["API", "Credentials", "MyGeotabException", "AuthenticationException"]
