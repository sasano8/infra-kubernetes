local typedefs = require "kong.db.schema.typedefs"
return {
  name = "session-auth",
  fields = {
    { consumer = typedefs.no_consumer },
    { protocols = typedefs.protocols_http },
    { config = { type = "record", fields = {
      { hosts = { type = "array", required = true, elements = {type = "string"} } },
      { mode = { type = "string", required = true, one_of = { "browser", "api", "oauth", "public" } } },
      { email_domain = { type = "string", required = true } },
      { https_port = { type = "integer", default = 443, between = {1, 65535} } },
      { api_paths = { type = "array", default = {"/api", "/upload"}, elements = {type = "string"} } },
    } } },
  },
}
