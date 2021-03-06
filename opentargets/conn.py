"""
This module abstracts the connection to the Open Targets REST API to simplify its usage.
Can be used directly but requires some knowledge of the API.
"""


import json
import logging
from collections import namedtuple
from itertools import islice

import requests
from cachecontrol import CacheControl
from hyper.contrib import HTTP20Adapter
import time
import namedtupled
from future.utils import implements_iterator
import yaml


VERSION=1.2

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

class HTTPMethods(object):
    GET='get'
    POST='post'


def result_to_json(result, **kwargs):
    '''transforms a result back to json. kwargs will be passed to json.dumps'''
    return json.dumps(result._asdict(), **kwargs)

class Response(object):
    ''' Handler for responses from the api'''

    def __init__(self, response, content_type = 'json'):
        self._logger =logging.getLogger(__name__)
        if content_type == 'json':
            parsed_response = response.json()
            if isinstance(parsed_response, dict):
                if 'data' in parsed_response:
                    self.data = parsed_response['data']
                    del parsed_response['data']
                else:
                    self.data = []
                if 'from' in parsed_response:
                    parsed_response['from_'] = parsed_response['from']
                    del parsed_response['from']
                self.info = self._dict_to_namedtuple(parsed_response, classname='ResultInfo')

            else:
                self.data = parsed_response
                self.info = {}
            self._headers = response.headers
            self._parse_usage_data()

        else:
            raise AttributeError("content-type not supported")

    @staticmethod
    def _dict_to_namedtuple(d, classname ='Result', rename = True):
        return namedtuple(classname, d.keys(),rename=rename)(*d.values())

    @staticmethod
    def _dict_to_nested_namedtuple(d, classname ='Result', ):
        return namedtupled.map(d,classname)

    def __str__(self):
        data = str(self.data)
        return data[:100] + (data[100:] and '...')


    def _parse_usage_data(self):
        if 'X-Usage-Limit-1h' in self._headers:
            UL1H = 'X-Usage-Limit-1h'
            UL10S = 'X-Usage-Limit-10s'
            UR1H = 'X-Usage-Remaining-1h'
            UR10S = 'X-Usage-Remaining-10s'
        elif b'X-Usage-Limit-1h' in self._headers:
            UL1H = b'X-Usage-Limit-1h'
            UL10S = b'X-Usage-Limit-10s'
            UR1H = b'X-Usage-Remaining-1h'
            UR10S = b'X-Usage-Remaining-10s'
        else:
            self.usage = None
            return

        usage = dict(limit={'hour': int(self._headers[UL1H]),
                            'seconds_10': int(self._headers[UL10S]),
                            },
                     remaining = {'hour': int(self._headers[UR1H]),
                                  'seconds_10': int(self._headers[UR10S]),
                                  },
                     )
        usage['remaining']['minimum'] =min((usage['remaining']['hour'], usage['remaining']['seconds_10']))

        usage['exceeded'] = usage['remaining']['minimum'] < 0
        self.usage = self._dict_to_nested_namedtuple(usage, classname='UsageInfo')
        if self.usage.exceeded:
            self._logger.warning('Fair Usage limit exceeded')
    def __len__(self):
        try:
            return self.info.total
        except:
            return len(self.data)


class Connection(object):

    _AUTO_GET_TOKEN = 'auto'

    def __init__(self,
                 host='https://www.targetvalidation.org',
                 port=443,
                 api_version='latest',
                 auth_app_name = None,
                 auth_secret = None,
                 use_http2=False,
                 ):
        '''

        :param host: host to point to
        :param port: port to use for connection
        :param api_version: api version, default to latest
        :param auth_app_name: app_name if using auth
        :param auth_secret: secret if using auth
        :param use_http2: activate http2 client
        '''
        self._logger = logging.getLogger(__name__)
        self.host = host
        self.port = port
        self.api_version = api_version
        self.auth_app_name = auth_app_name
        self.auth_secret = auth_secret
        if self.auth_app_name and self.auth_secret:
            self.use_auth = True
        else:
            self.use_auth = False
        self.token = None
        self.use_http2 = use_http2
        session= requests.Session()
        if self.use_http2:
            session.mount(host, HTTP20Adapter())
        self.session = CacheControl(session)
        self._get_remote_api_specs()



    def _build_url(self, endpoint):
        return '{}:{}/api/{}{}'.format(self.host,
                                       self.port,
                                       self.api_version,
                                       endpoint,)

    def get(self, endpoint, params=None):
        return Response(self._make_request(endpoint,
                              params=params,
                              method='GET'))

    def post(self, endpoint, data=None):
        return Response(self._make_request(endpoint,
                               data=data,
                               method='POST'))


    def _make_token_request(self, expire = 10*60):
        return self._make_request('/public/auth/request_token',
                                  data={'app_name':self.auth_app_name,
                                        'secret':self.auth_secret,
                                        'expiry': expire},
                                  )

    def get_token(self, expire = 10*60):
        response = self._make_token_request(expire)
        return response.json()['token']

    def _make_request(self,
                      endpoint,
                      params = None,
                      data = None,
                      method = "GET",
                      headers = {},
                      rate_limit_fail = False,
                      **kwargs):

        def call():
            return self.session.request(method,
                                    self._build_url(endpoint),
                                    params = params,
                                    data = data,
                                    headers = headers,
                                    **kwargs)

        'order params to allow efficient caching'
        if params is not None:
            if isinstance(params, dict):
                params = sorted(params.items())
            else:
                params = sorted(params)

        if self.use_auth and not 'request_token' in endpoint:
            if self.token is None:
                self._update_token()
            if self.token is not None:
                headers['Auth-Token']=self.token

        response = None
        if not rate_limit_fail:
            status_code = 429
            while status_code in [429,419]:
                response = call()
                status_code = response.status_code
                if status_code == 429:
                    retry_after=1
                    if 'Retry-After' in response.headers:
                        retry_after = float(response.headers['Retry-After'])
                    self._logger.warning('Maximum usage limit hit. Retrying in {} seconds'.format(retry_after))
                    time.sleep(retry_after)
                elif  status_code == 419:
                    self._update_token()
        else:
            response = call()

        response.raise_for_status()
        return response

    def _update_token(self):
        if self.token:
            token_valid_response = self._make_request('/public/auth/validate_token',
                                                       headers={'Auth-Token':self.token})
            if token_valid_response.status_code == 200:
                return
            elif token_valid_response.status_code == 419:
                pass
            else:
                token_valid_response.raise_for_status()

        self.token = self.get_token()

    def _get_remote_api_specs(self):
        r= self.session.get(self.host+'/api/docs/swagger.yaml')
        r.raise_for_status()
        self.swagger_yaml = r.text
        self.api_specs = yaml.load(self.swagger_yaml)
        self.endpoint_validation_data={}
        for p, data in self.api_specs['paths'].items():
            p=p.split('{')[0]
            if p[-1]== '/':
                p=p[:-1]
            self.endpoint_validation_data[p] = {}
            for method, method_data in data.items():
                if 'parameters' in method_data:
                    params = {}
                    for par in method_data['parameters']:
                        par_type = par.get('type', 'string')
                        params[par['name']]=par_type
                    self.endpoint_validation_data[p][method] = params


        remote_version = self.get('/public/utils/version').data
        if remote_version != VERSION:
            self._logger.warning('The remote server is running the API with version {}, but the client expected {}. They may not be compatible.'.format(remote_version, VERSION))

    def validate_parameter(self, endpoint, filter_type, value, method=HTTPMethods.GET):
        endpoint_data = self.endpoint_validation_data[endpoint][method]
        if filter_type in endpoint_data:
            if endpoint_data[filter_type] == 'string' and isinstance(value, str):
                return
            elif endpoint_data[filter_type] == 'boolean' and isinstance(value, bool):
                return
            elif endpoint_data[filter_type] == 'number' and isinstance(value, (int, float)):
                return

        raise AttributeError('{}={} is not a valid parameter for endpoint {}'.format(filter_type, value, endpoint))

    def close(self):
        self.session.close()

@implements_iterator
class IterableResult(object):
    '''
    proxy over the Connection class that allows to iterate over all the items returned from a query making multiple calls to the backend in API background
    '''
    def __init__(self, conn, method = HTTPMethods.GET):
        self.conn = conn
        self.method = method

    def __call__(self, *args, **kwargs):

        self._args = args
        self._kwargs = kwargs
        response = self._make_call()
        self.info = response.info
        self._data = response.data
        self.current = 0
        try:
            self.total = int(self.info.total)
        except:
            self.total = len(self._data)
        return self

    def filter(self, **kwargs):
        if kwargs:
            for filter_type, filter_value in kwargs.items():
                self._validate_filter(filter_type, filter_value)
                self._kwargs['params'][filter_type] = filter_value
            self.__call__(*self._args, **self._kwargs)
        return self


    def _make_call(self):
        if self.method == HTTPMethods.GET:
            return self.conn.get(*self._args, **self._kwargs)
        elif self.method == HTTPMethods.POST:
            return self.conn.post(*self._args, **self._kwargs)
        else:
            raise AttributeError("HTTP method {} is not supported".format(self.method))

    def __iter__(self):
        return self

    def __next__(self):
        if self.current < self.total:
            if not self._data:
                self._kwargs['params']['from'] = self.current
                self._data = self._make_call().data
            d = self._data.pop(0)
            self.current+=1
            return d
        else:
            raise StopIteration()

    def __len__(self):
        try:
            return self.total
        except:
            return 0

    def __bool__(self):
        return self.__len__() >0

    def __nonzero__(self):
        return self.__bool__()

    def __str__(self):
        try:
            return_str = '{} Results found'.format(self.info.total)
            if  self._kwargs['params']:
                return_str+=' | parameters: {}'.format(self._kwargs['params'])
            return return_str
        except:
            data = str(self._data)
            return data[:100] + (data[100:] and '...')

    def __getitem__(self, x):
        if type(x) is slice:
            return list(islice(self, x.start, x.stop, x.step))
        else:
            return next(islice(self, x, None), None)

    def _validate_filter(self,filter_type, value):
        self.conn.validate_parameter(self._args[0], filter_type, value)



if __name__=='__main__':
    conn= Connection(host='https://mirror.targetvalidation.org')

    print(conn._build_url('/public/search'))
    '''test cache'''
    for i in range(5):
        start_time = time.time()
        conn.get('/public/search', {'q':'braf'})
        print(time.time()-start_time, 'seconds')
    '''test response'''
    r=conn.get('/public/search', {'q':'braf'})
    print(r.usage.remaining.minimum)
    print(r.info)
    print(r.usage)
    for i,d in enumerate(r.data):
        print(i,d.id, d.type, d.data['approved_symbol'])