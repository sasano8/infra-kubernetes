local typedefs = require "kong.db.schema.typedefs"
local function valid_id_token_header(header)
  if not header:match("^[Xx]%-[A-Za-z0-9][A-Za-z0-9%-]*$") then
    return false, "expected a dedicated X- header name"
  end
  local name = header:lower()
  if name == "x-forwarded-id-token" then return true end
  for _, prefix in ipairs({"x-forwarded-", "x-auth-request-", "x-kong-", "x-original-"}) do
    if name:sub(1, #prefix) == prefix then
      return false, "routing and identity header names are reserved"
    end
  end
  if name == "x-real-ip" then return false, "routing header name is reserved" end
  return true
end

return {
  name = "session-auth",
  fields = {
    { consumer = typedefs.no_consumer },
    { protocols = typedefs.protocols_http },
    { config = { type = "record", fields = {
      { hosts = { type = "array", required = true, elements = {type = "string"} } },
      { auth_host = { type = "string", required = true } },
      { mode = { type = "string", required = true, one_of = { "sso", "api", "oauth", "public" } } },
      { email_domain = { type = "string", required = true } },
      { https_port = { type = "integer", default = 443, between = {1, 65535} } },
      { api_paths = { type = "array", default = {"/api", "/upload"}, elements = {type = "string"} } },
      { forward_id_token = { type = "boolean", default = false } },
      { id_token_header = { type = "string", default = "X-Forwarded-Id-Token",
                            custom_validator = valid_id_token_header } },
    } } },
  },
}
