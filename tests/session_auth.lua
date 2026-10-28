-- Execute the real handler against a fake Kong PDK/auth service. No credentials or network.
local response, calls, request, sent, cleared, outgoing, result, response_headers

package.preload['resty.http'] = function()
  return {
    new = function()
      return {
        set_timeouts = function() end,
        request_uri = function(self, url, opts)
          calls = calls + 1
          sent = opts
          assert(opts.method == 'GET')
          assert(opts.body == nil)
          return response
        end
      }
    end
  }
end

ngx = {
  escape_uri = function(s)
    return (s:gsub('[^%w%-_.~]', function(c)
      return string.format('%%%02X', string.byte(c))
    end))
  end
}

local handler = dofile('plugins/sso/kong/plugins/session-auth/handler.lua')
local conf = {
  hosts = {'app.example.test'},
  mode = 'sso',
  email_domain = 'example.test',
  https_port = 443,
  auth_host = 'auth.knative.example.test',
  api_paths = {'/api', '/upload'}
}

local function setup()
  response = {
    status = 202,
    headers = {
      ['X-Auth-Request-User'] = 'user',
      ['X-Auth-Request-Email'] = 'user@example.test'
    }
  }
  calls = 0
  sent = nil
  cleared = {}
  outgoing = {}
  result = nil
  request = {
    host = 'app.example.test',
    authority = 'app.example.test',
    scheme = 'https',
    method = 'GET',
    path = '/get',
    cookie = '__Secure-infra_sso=session; app=value',
    headers = {}
  }
  response_headers = setmetatable({}, {
    __index = function(headers, name)
      return rawget(headers, name:lower())
    end
  })

  -- Kong 3.9.1 access.before initializes these from $host (which excludes the port)
  -- and the listener. nginx_kong.lua uses these variables for proxy_set_header.
  ngx.var = {
    upstream_x_forwarded_host = request.host,
    upstream_x_forwarded_proto = 'https',
    upstream_x_forwarded_port = '443'
  }
  kong = {
    ctx = {plugin = {}},
    request = {
      get_host = function()
        return request.host
      end,
      get_scheme = function()
        return request.scheme
      end,
      get_method = function()
        return request.method
      end,
      get_path = function()
        return request.path
      end,
      get_path_with_query = function()
        return request.path .. '?q=1'
      end,
      get_header = function(n)
        if n == 'Cookie' then
          return request.cookie
        end
        return request.headers[n:lower()]
      end
    },
    service = {
      request = {
        clear_header = function(n)
          cleared[n] = true
          request.headers[n:lower()] = nil
          outgoing[n] = nil
        end,
        set_header = function(n, v)
          outgoing[n] = v
        end
      }
    },
    response = {
      exit = function(s, b, h)
        result = {status = s, body = b, headers = h}
        return result
      end,
      -- The real response PDK returns only the first value from get_header.
      get_header = function(n)
        local value = response_headers[n]
        if type(value) == 'table' then
          return value[1]
        end
        return value
      end,
      get_headers = function()
        return response_headers
      end,
      set_header = function(n, v)
        response_headers[n:lower()] = v
        outgoing[n] = v
      end
    }
  }
  conf.mode = 'sso'
  conf.hosts = {'app.example.test'}
  conf.auth_host = 'auth.knative.example.test'
  conf.https_port = 443
  conf.api_paths = {'/api', '/upload'}
  conf.forward_id_token = false
  conf.id_token_header = 'X-Forwarded-Id-Token'
end

local function effective_upstream_headers()
  local headers = {}
  for name, value in pairs(outgoing) do
    headers[name] = value
  end

  -- ngx.req.set_header (the service.request.set_header implementation for these
  -- names) does not override these explicit Nginx proxy_set_header directives.
  headers['X-Forwarded-Host'] = ngx.var.upstream_x_forwarded_host
  headers['X-Forwarded-Proto'] = ngx.var.upstream_x_forwarded_proto
  headers['X-Forwarded-Port'] = ngx.var.upstream_x_forwarded_port
  return headers
end

local function oauth_callback_uri()
  local headers = effective_upstream_headers()
  -- oauth2-proxy's reverse-proxy mode prefers X-Forwarded-Host over Host and
  -- does not append X-Forwarded-Port when constructing its redirect URI. The
  -- configured --cookie-secure=true forces the redirect scheme to HTTPS.
  return 'https://' .. (headers['X-Forwarded-Host'] or request.authority) .. '/oauth2/callback'
end

local count, failures = 0, 0

local function test(name, fn)
  setup()
  local ok, err = pcall(fn)
  if ok then
    count = count + 1
    print('PASS ' .. name)
  else
    failures = failures + 1
    print('FAIL ' .. name .. ': ' .. tostring(err))
  end
end

test('authenticated bodyless check and isolated cookies', function()
  handler:access(conf)

  assert(result == nil and calls == 1)
  assert(sent.headers.Cookie == '__Secure-infra_sso=session')
  assert(outgoing.Cookie == ' app=value')
  assert(outgoing['X-Auth-Request-Email'] == 'user@example.test')
  assert(cleared['X-Forwarded-User'])
end)

test('sso redirect preserves URL', function()
  response.status = 401

  handler:access(conf)

  assert(result.status == 302)
  assert(result.headers.Location:find('https%%3A%%2F%%2Fapp'))
end)

for _, method in ipairs({'POST', 'PUT', 'PATCH', 'DELETE'}) do
  test('unsafe ' .. method .. ' is never replayed', function()
    request.method = method
    response.status = 401

    handler:access(conf)

    assert(result.status == 401)
  end)
end

test('api prefix GET receives 401', function()
  request.path = '/api/items'
  response.status = 401

  handler:access(conf)

  assert(result.status == 401)
end)

test('exact API path GET receives 401', function()
  request.path = '/api'
  response.status = 401

  handler:access(conf)

  assert(result.status == 401)
end)

test('similar path outside the API prefix still redirects', function()
  request.path = '/apiculture'
  response.status = 401

  handler:access(conf)

  assert(result.status == 302)
end)

test('custom exact API path GET receives 401', function()
  conf.api_paths = {'/get'}
  response.status = 401

  handler:access(conf)

  assert(result.status == 401)
end)

test('custom API prefix requires a path boundary', function()
  conf.api_paths = {'/ge'}
  response.status = 401

  handler:access(conf)

  assert(result.status == 302)
end)

test('api mode receives 401', function()
  conf.mode = 'api'
  response.status = 401

  handler:access(conf)

  assert(result.status == 401)
end)

test('auth outage fails closed', function()
  response = nil

  handler:access(conf)

  assert(result.status == 503)
end)

test('unexpected auth response fails closed', function()
  response.status = 302

  handler:access(conf)

  assert(result.status == 503)
end)

test('missing identity fails closed', function()
  response.headers = {}

  handler:access(conf)

  assert(result.status == 503)
end)

test('foreign email domain refused', function()
  response.headers['X-Auth-Request-Email'] = 'user@other.test'

  handler:access(conf)

  assert(result.status == 403)
  assert(result.body.message == 'Access denied' and result.body.error == nil)
end)

test('auth forbidden stays forbidden', function()
  response.status = 403

  handler:access(conf)

  assert(result.status == 403)
end)

test('unknown host refused before auth', function()
  request.host = 'evil.test'

  handler:access(conf)

  assert(result.status == 403 and calls == 0)
end)

test('plain HTTP refused', function()
  request.scheme = 'http'

  handler:access(conf)

  assert(result.status == 403 and calls == 0)
end)

test('refresh split cookies without upstream cookie', function()
  response.headers['Set-Cookie'] = {'a=1', 'b=2'}

  handler:access(conf)
  handler:header_filter(conf)

  assert(#outgoing['Set-Cookie'] == 2)
end)

test('refresh preserves application cookies', function()
  response.headers['Set-Cookie'] = 'session=new'
  handler:access(conf)

  response_headers['set-cookie'] = 'app=old'
  handler:header_filter(conf)

  assert(
    #outgoing['Set-Cookie'] == 2
    and outgoing['Set-Cookie'][1] == 'app=old'
    and outgoing['Set-Cookie'][2] == 'session=new'
  )
end)

test('refresh preserves multiple application and split authentication cookies', function()
  response.headers['Set-Cookie'] = {
    '__Secure-infra_sso_0=new0; Secure',
    '__Secure-infra_sso_1=new1; Secure'
  }
  handler:access(conf)

  response_headers['set-cookie'] = {'app_session=old; Secure', 'app_csrf=token; Secure'}
  assert(kong.response.get_header('Set-Cookie') == 'app_session=old; Secure')
  handler:header_filter(conf)

  local cookies = outgoing['Set-Cookie']
  assert(#cookies == 4, 'expected both application cookies and both split authentication cookies')
  assert(cookies[1] == 'app_session=old; Secure' and cookies[2] == 'app_csrf=token; Secure')
  assert(cookies[3] == '__Secure-infra_sso_0=new0; Secure' and cookies[4] == '__Secure-infra_sso_1=new1; Secure')
end)

test('application cookies are unchanged when authentication does not refresh', function()
  response_headers['set-cookie'] = {'app_session=old', 'app_csrf=token'}

  handler:access(conf)
  handler:header_filter(conf)

  assert(#response_headers['set-cookie'] == 2 and response_headers['set-cookie'][2] == 'app_csrf=token')
end)

test('public route strips spoofed identity but does not authenticate', function()
  conf.mode = 'public'

  handler:access(conf)

  assert(calls == 0 and result == nil and cleared['X-Auth-Request-Email'])
end)

test('oauth root is not a special public landing page', function()
  conf.mode = 'oauth'
  request.host = conf.auth_host
  conf.hosts = {conf.auth_host}
  request.path = '/'
  handler:access(conf)
  assert(result.status == 404 and calls == 0)
  assert(result.body.error == 'path_not_found' and result.body.message == nil)
end)

test('public route accepts one wildcard label and strips session cookies', function()
  conf.mode = 'public'
  conf.hosts = {'*.knative-public.example.test'}
  request.host = 'sso-test-ksvc.knative-public.example.test'
  request.cookie = '__Secure-infra_sso=private; app=visible'
  handler:access(conf)
  assert(calls == 0 and result == nil)
  assert(outgoing['Cookie'] == ' app=visible')
end)

test('public wildcard does not accept nested or unrelated hosts', function()
  conf.mode = 'public'
  conf.hosts = {'*.knative-public.example.test'}
  request.host = 'nested.app.knative-public.example.test'
  handler:access(conf)
  assert(result.status == 403 and calls == 0)
end)

test('oauth internal authentication endpoint remains private', function()
  conf.mode = 'oauth'
  request.host = conf.auth_host
  conf.hosts = {conf.auth_host}
  request.path = '/oauth2/auth'

  handler:access(conf)

  assert(result.status == 404)
  assert(result.body.error == 'path_not_found' and result.body.message == nil)
end)

test('oauth callback uses the effective HTTPS authority on port 443', function()
  conf.mode = 'oauth'
  request.host = conf.auth_host
  conf.hosts = {conf.auth_host}
  request.path = '/oauth2/callback'

  handler:access(conf)

  assert(result == nil and calls == 0)
  assert(oauth_callback_uri() == 'https://auth.knative.example.test/oauth2/callback')
  assert(effective_upstream_headers()['X-Forwarded-Port'] == '443')
end)

test('oauth start preserves port 9443 in the effective callback authority', function()
  conf.mode = 'oauth'
  request.host = conf.auth_host
  conf.hosts = {conf.auth_host}
  conf.https_port = 9443
  request.path = '/oauth2/start'
  request.authority = 'app.example.test:9443'
  ngx.var.upstream_x_forwarded_port = '9443'

  handler:access(conf)

  assert(result == nil and calls == 0)
  assert(
    oauth_callback_uri() == 'https://auth.knative.example.test:9443/oauth2/callback',
    'effective X-Forwarded-Host lost the HTTPS port'
  )
end)

test('oauth callback preserves port 9443 in effective forwarded headers', function()
  conf.mode = 'oauth'
  request.host = conf.auth_host
  conf.hosts = {conf.auth_host}
  conf.https_port = 9443
  request.path = '/oauth2/callback'
  request.authority = 'app.example.test:9443'
  ngx.var.upstream_x_forwarded_port = '9443'

  handler:access(conf)

  assert(result == nil and calls == 0)
  local headers = effective_upstream_headers()
  assert(headers['X-Forwarded-Host'] == 'auth.knative.example.test:9443', 'Nginx still forwards the portless core host')
  assert(headers['X-Forwarded-Proto'] == 'https' and headers['X-Forwarded-Port'] == '9443')
end)

test('oauth callback discards spoofed forwarding headers from a trusted peer', function()
  conf.mode = 'oauth'
  request.host = conf.auth_host
  conf.hosts = {conf.auth_host}
  conf.https_port = 9443
  request.path = '/oauth2/callback'
  request.authority = 'app.example.test:9443'
  request.headers = {
    ['x-forwarded-host'] = 'evil.test',
    ['x-forwarded-proto'] = 'http',
    ['x-forwarded-port'] = '80'
  }
  -- access.before accepts these from a configured trusted peer; the auth plugin
  -- must still enforce its own raw TLS listener and matched-host policy.
  ngx.var.upstream_x_forwarded_host = 'evil.test'
  ngx.var.upstream_x_forwarded_proto = 'http'
  ngx.var.upstream_x_forwarded_port = '80'

  handler:access(conf)

  assert(result == nil and calls == 0)
  assert(
    oauth_callback_uri() == 'https://auth.knative.example.test:9443/oauth2/callback',
    'spoofed authority survived in effective Nginx headers'
  )
  local headers = effective_upstream_headers()
  assert(headers['X-Forwarded-Proto'] == 'https' and headers['X-Forwarded-Port'] == '9443')
end)

test('sso redirect preserves the configured HTTPS port', function()
  conf.https_port = 9443
  response.status = 401

  handler:access(conf)

  assert(result.status == 302 and result.headers.Location:find('app.example.test%3A9443', 1, true))
end)

-- These JWT-shaped fixtures are not signed tokens. Google verification belongs
-- to oauth2-proxy and the receiving application, not this forwarding unit test.
local id_token = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl'

test('ID token forwarding is disabled by default and strips spoofed input', function()
  request.headers['x-forwarded-id-token'] = 'spoofed'
  response.headers.Authorization = 'Bearer ' .. id_token

  handler:access(conf)

  assert(result == nil and calls == 1)
  assert(cleared['X-Forwarded-Id-Token'])
  assert(request.headers['x-forwarded-id-token'] == nil)
  assert(outgoing['X-Forwarded-Id-Token'] == nil)
end)

test('authenticated ID token is forwarded without replacing API Authorization', function()
  conf.forward_id_token = true
  request.headers.authorization = 'Bearer application-api-token'
  request.headers['x-forwarded-id-token'] = {'spoofed-1', 'spoofed-2'}
  response.headers.Authorization = 'Bearer ' .. id_token

  handler:access(conf)
  handler:header_filter(conf)

  assert(result == nil and outgoing['X-Forwarded-Id-Token'] == id_token)
  assert(request.headers['x-forwarded-id-token'] == nil)
  assert(request.headers.authorization == 'Bearer application-api-token')
  assert(outgoing.Authorization == nil and not cleared.Authorization)
  assert(sent.headers.Authorization == nil and sent.headers['X-Forwarded-Id-Token'] == nil)
  assert(response_headers['x-forwarded-id-token'] == nil and response_headers.authorization == nil)
end)

test('configured ID token header replaces spoofed values and strips the default name', function()
  conf.forward_id_token = true
  conf.id_token_header = 'X-SSO-Identity'
  request.headers['x-sso-identity'] = {'spoofed-1', 'spoofed-2'}
  request.headers['x-forwarded-id-token'] = 'spoofed-default'
  response.headers.Authorization = 'Bearer ' .. id_token

  handler:access(conf)

  assert(result == nil and outgoing['X-SSO-Identity'] == id_token)
  assert(cleared['X-SSO-Identity'] and cleared['X-Forwarded-Id-Token'])
  assert(request.headers['x-sso-identity'] == nil and request.headers['x-forwarded-id-token'] == nil)
  assert(outgoing['X-Forwarded-Id-Token'] == nil)
end)

test('disabled route also strips a custom ID token header', function()
  conf.id_token_header = 'X-SSO-Identity'
  request.headers['x-sso-identity'] = 'spoofed'
  response.headers.Authorization = 'Bearer ' .. id_token

  handler:access(conf)

  assert(result == nil and cleared['X-SSO-Identity'])
  assert(request.headers['x-sso-identity'] == nil and outgoing['X-SSO-Identity'] == nil)
end)

for _, mode in ipairs({'public', 'oauth'}) do
  test(mode .. ' endpoint never forwards an ID token', function()
    conf.mode = mode
    conf.forward_id_token = true
    conf.id_token_header = 'X-SSO-Identity'
    if mode == 'oauth' then
      request.host = conf.auth_host
      conf.hosts = {conf.auth_host}
      request.path = '/oauth2/callback'
    end
    request.headers['x-sso-identity'] = 'spoofed'
    request.headers['x-forwarded-id-token'] = 'spoofed-default'
    response.headers.Authorization = 'Bearer ' .. id_token

    handler:access(conf)

    assert(result == nil and calls == 0)
    assert(request.headers['x-sso-identity'] == nil and request.headers['x-forwarded-id-token'] == nil)
    assert(outgoing['X-SSO-Identity'] == nil and outgoing['X-Forwarded-Id-Token'] == nil)
  end)
end

test('missing required ID token fails closed after successful authentication', function()
  conf.forward_id_token = true
  request.headers.authorization = 'Bearer ' .. id_token
  response.headers['X-Auth-Request-Access-Token'] = id_token
  response.headers['X-Forwarded-Access-Token'] = id_token

  handler:access(conf)

  assert(result and result.status == 503)
  assert(outgoing['X-Forwarded-Id-Token'] == nil)
end)

for index, authorization in ipairs({
  false,
  123,
  '',
  id_token,
  'Basic ' .. id_token,
  'Bearer ',
  'Bearer opaque-access-token',
  'Bearer ' .. id_token .. '\r\nInjected: true',
  'Bearer ' .. id_token .. ', Bearer ' .. id_token,
  {'Bearer ' .. id_token, 'Bearer ' .. id_token},
  {'Bearer ' .. id_token}
}) do
  test('malformed or ambiguous required ID token fails closed: case ' .. index, function()
    conf.forward_id_token = true
    response.headers.Authorization = authorization

    handler:access(conf)

    assert(result and result.status == 503)
    assert(outgoing['X-Forwarded-Id-Token'] == nil)
  end)
end

for _, status in ipairs({401, 403, 500}) do
  test('auth status ' .. status .. ' never forwards an ID token', function()
    conf.forward_id_token = true
    request.method = 'POST'
    response.status = status
    response.headers.Authorization = 'Bearer ' .. id_token

    handler:access(conf)

    assert(result and result.status == (status == 500 and 503 or status))
    assert(outgoing['X-Forwarded-Id-Token'] == nil)
  end)
end

test('foreign identity never receives an ID token even with a successful auth response', function()
  conf.forward_id_token = true
  response.headers.Authorization = 'Bearer ' .. id_token
  response.headers['X-Auth-Request-Email'] = 'user@other.test'

  handler:access(conf)

  assert(result.status == 403 and outgoing['X-Forwarded-Id-Token'] == nil)
end)

test('auth outage strips spoofed ID tokens and fails closed', function()
  conf.forward_id_token = true
  request.headers['x-forwarded-id-token'] = 'spoofed'
  response = nil

  handler:access(conf)

  assert(result.status == 503 and outgoing['X-Forwarded-Id-Token'] == nil)
  assert(request.headers['x-forwarded-id-token'] == nil)
end)

test('missing identity never forwards an ID token', function()
  conf.forward_id_token = true
  response.headers.Authorization = 'Bearer ' .. id_token
  response.headers['X-Auth-Request-User'] = nil

  handler:access(conf)

  assert(result.status == 503 and outgoing['X-Forwarded-Id-Token'] == nil)
end)

test('refreshed ID token and split cookies are forwarded to their separate destinations', function()
  conf.forward_id_token = true
  response.headers.Authorization = 'Bearer ' .. id_token
  response.headers['Set-Cookie'] = {'__Secure-infra_sso_0=refresh', '__Secure-infra_sso_1=refresh'}

  handler:access(conf)
  handler:header_filter(conf)

  assert(result == nil and outgoing['X-Forwarded-Id-Token'] == id_token)
  assert(#response_headers['set-cookie'] == 2)
  assert(response_headers['x-forwarded-id-token'] == nil and response_headers.authorization == nil)
end)

test('plugin schema rejects reserved and malformed ID token headers', function()
  package.preload['kong.db.schema.typedefs'] = function()
    return {no_consumer = {}, protocols_http = {}}
  end
  local schema = dofile('plugins/sso/kong/plugins/session-auth/schema.lua')
  local validator
  for _, field in ipairs(schema.fields[3].config.fields) do
    if field.id_token_header then
      validator = field.id_token_header.custom_validator
    end
  end
  assert(type(validator) == 'function', 'ID token header validator missing from plugin schema')
  for _, header in ipairs({
    'Authorization', 'Cookie', 'Host', 'X Token', 'X_Token', 'X-Token\r\nHost',
    'X-Forwarded-Host', 'x-forwarded-proto', 'X-Auth-Request-Email',
    'X-Kong-Request-Debug', 'X-Original-URI', 'X-Real-IP'
  }) do
    assert(not validator(header), 'reserved or malformed header accepted')
  end
  for _, header in ipairs({'X-Forwarded-Id-Token', 'x-forwarded-id-token', 'X-SSO-ID-Token', 'x-company-identity'}) do
    assert(validator(header), 'dedicated ID token header rejected')
  end
end)

test('all application hosts use one login origin and keep their own return URL', function()
  response.status = 401
  conf.hosts = {'app.example.test', 'other.knative.example.test'}
  for _, host in ipairs(conf.hosts) do
    request.host = host
    handler:access(conf)
    local target = 'https://' .. host .. '/get?q=1'
    assert(result.status == 302)
    assert(result.headers.Location == 'https://auth.knative.example.test/oauth2/start?rd=' .. ngx.escape_uri(target))
  end
end)

test('the shared session is checked for both standard and Knative hosts', function()
  conf.hosts = {'app.example.test', 'other.knative.example.test'}
  for _, host in ipairs(conf.hosts) do
    request.host = host
    handler:access(conf)
    assert(result == nil and sent.headers.Cookie == '__Secure-infra_sso=session')
    assert(outgoing.Cookie == ' app=value')
  end
  assert(calls == 2)
end)

test('old host-only cookies are stripped but cannot authenticate a request', function()
  request.cookie = '__Host-infra_sso=old; __Host-infra_sso_0=old-part; app=value'
  response.status = 401
  handler:access(conf)
  assert(result.status == 302 and sent.headers.Cookie == '')
  assert(outgoing.Cookie == ' app=value')
end)

test('shared split session and CSRF cookies never reach an application', function()
  request.cookie = '__Secure-infra_sso_0=part0; __Secure-infra_sso_1=part1; __Secure-infra_sso_csrf=csrf; app=value'
  handler:access(conf)
  assert(result == nil and outgoing.Cookie == ' app=value')
  assert(sent.headers.Cookie:find('__Secure-infra_sso_0=part0', 1, true))
  assert(sent.headers.Cookie:find('__Secure-infra_sso_1=part1', 1, true))
end)

test('OAuth paths are refused on an application host even if allowlisted', function()
  conf.mode = 'oauth'
  conf.hosts = {'app.example.test', conf.auth_host}
  request.path = '/oauth2/callback'
  handler:access(conf)
  assert(result.status == 403 and calls == 0)
end)

test('a forged forwarding host cannot change the central login or return host', function()
  response.status = 401
  request.headers['x-forwarded-host'] = 'evil.test'
  handler:access(conf)
  assert(result.headers.Location == 'https://auth.knative.example.test/oauth2/start?rd=' .. ngx.escape_uri('https://app.example.test/get?q=1'))
end)

test('central login and return URL both retain the configured HTTPS port', function()
  conf.https_port = 9443
  response.status = 401
  handler:access(conf)
  assert(result.headers.Location == 'https://auth.knative.example.test:9443/oauth2/start?rd=' .. ngx.escape_uri('https://app.example.test:9443/get?q=1'))
end)

print('Lua handler tests: ' .. count .. ' passed')
if failures > 0 then
  error(failures .. ' Lua handler tests failed')
end
