local http = require "resty.http"
local Handler = { PRIORITY = 1500, VERSION = "1.1.0" }
local default_id_token_header = "X-Forwarded-Id-Token"
local identity = { "X-Auth-Request-User", "X-Auth-Request-Email", "X-Auth-Request-Groups",
                   "X-Auth-Request-Preferred-Username", "X-Forwarded-User", "X-Forwarded-Email",
                   "X-Forwarded-Groups", "X-Forwarded-Access-Token", "X-Auth-Request-Access-Token" }
local function deny(status)
  return kong.response.exit(status, { message = status == 401 and "Authentication required" or "Access denied" },
                            { ["Cache-Control"] = "no-store" })
end
function Handler:access(conf)
  local host = kong.request.get_host()
  local allowed = false
  for _, h in ipairs(conf.hosts) do if h == host then allowed = true end end
  if not allowed then return deny(403) end
  -- Trust only the TLS listener and the matched route, never client forwarding headers.
  if kong.request.get_scheme() ~= "https" then return deny(403) end
  local authority = host .. (conf.https_port == 443 and "" or ":" .. conf.https_port)
  for _, name in ipairs(identity) do kong.service.request.clear_header(name) end
  -- Strip both names even on public/OAuth routes or when forwarding is disabled.
  -- A client-supplied ID token must never be mistaken for our auth result.
  local id_token_header = conf.id_token_header or default_id_token_header
  kong.service.request.clear_header(default_id_token_header)
  kong.service.request.clear_header(id_token_header)
  kong.service.request.clear_header("Forwarded")
  kong.service.request.clear_header("X-Auth-Request-Redirect")
  kong.service.request.set_header("X-Forwarded-Proto", "https")
  kong.service.request.set_header("X-Forwarded-Host", authority)
  kong.service.request.set_header("X-Forwarded-Port", tostring(conf.https_port))
  -- Kong 3.9.1's Nginx template builds the actual proxy headers from these
  -- variables; service.request.set_header alone does not replace their values.
  ngx.var.upstream_x_forwarded_proto = "https"
  ngx.var.upstream_x_forwarded_host = authority
  ngx.var.upstream_x_forwarded_port = tostring(conf.https_port)
  if conf.mode == "oauth" then
    local path = kong.request.get_path()
    local public = { ["/oauth2/start"]=true, ["/oauth2/callback"]=true,
                     ["/oauth2/sign_in"]=true, ["/oauth2/sign_out"]=true }
    if not public[path] then return deny(404) end
    return
  end
  local auth_cookies, app_cookies = {}, {}
  for part in (kong.request.get_header("Cookie") or ""):gmatch("[^;]+") do
    local name = part:match("^%s*([^=]+)=") or ""
    if name:sub(1, #"__Host-infra_sso") == "__Host-infra_sso" then
      auth_cookies[#auth_cookies+1] = part
    else app_cookies[#app_cookies+1] = part end
  end
  -- Do not expose the SSO session to applications, echo endpoints, or application logs.
  kong.service.request.clear_header("Cookie")
  if #app_cookies > 0 then kong.service.request.set_header("Cookie", table.concat(app_cookies, ";")) end
  if conf.mode == "public" then return end
  -- GET with Cookie only: do not read, buffer, replay, or forward the request body or Bearer token.
  local client = http.new()
  client:set_timeouts(2000, 2000, 5000)
  local res = client:request_uri("http://oauth2-proxy.test.svc.cluster.local:4180/oauth2/auth", {
    method = "GET", redirect = false,
    headers = { Cookie = table.concat(auth_cookies, ";"), Host = host,
                ["X-Forwarded-Proto"] = "https", ["X-Forwarded-Host"] = host },
    keepalive = true,
  })
  if not res then return deny(503) end
  -- Preserve every split/refresh cookie, including on authentication failures.
  kong.ctx.plugin.cookies = res.headers["Set-Cookie"]
  if res.status == 202 then
    local email = res.headers["X-Auth-Request-Email"]
    local user = res.headers["X-Auth-Request-User"]
    if type(email) ~= "string" or email == "" or type(user) ~= "string" or user == "" then
      return deny(503)
    end
    if email:sub(-(#conf.email_domain + 1)):lower() ~= "@" .. conf.email_domain then return deny(403) end
    if conf.forward_id_token then
      -- --set-authorization-header returns the session's Google ID token.
      -- Do not use the client's Authorization or OAuth access-token headers.
      local authorization = res.headers["Authorization"]
      local token = type(authorization) == "string"
                    and authorization:match("^Bearer ([A-Za-z0-9_-]+%.[A-Za-z0-9_-]+%.[A-Za-z0-9_-]+)$")
      if not token then return deny(503) end
      kong.service.request.set_header(id_token_header, token)
    end
    kong.service.request.set_header("X-Auth-Request-User", user)
    kong.service.request.set_header("X-Auth-Request-Email", email)
    return
  end
  if res.status == 403 then return deny(403) end
  if res.status ~= 401 then return deny(503) end
  local method = kong.request.get_method()
  local path = kong.request.get_path()
  local api = conf.mode == "api" or (method ~= "GET" and method ~= "HEAD") or kong.request.get_header("Upgrade") ~= nil
  for _, prefix in ipairs(conf.api_paths) do
    if path == prefix or path:sub(1, #prefix + 1) == prefix .. "/" then api = true end
  end
  if api then return deny(401) end
  local target = "https://" .. authority .. kong.request.get_path_with_query()
  return kong.response.exit(302, "", { Location = "/oauth2/start?rd=" .. ngx.escape_uri(target),
                                       ["Cache-Control"] = "no-store" })
end
function Handler:header_filter(conf)
  local cookies = kong.ctx.plugin.cookies
  if cookies then
    local merged = {}
    -- get_header returns only the first cookie; get_headers preserves them all.
    local existing = kong.response.get_headers()["set-cookie"]
    local function append(value)
      if type(value) == "table" then for _, cookie in ipairs(value) do merged[#merged+1] = cookie end
      elseif value then merged[#merged+1] = value end
    end
    append(existing)
    append(cookies)
    kong.response.set_header("Set-Cookie", merged)
  end
end
return Handler
