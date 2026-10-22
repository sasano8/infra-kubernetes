-- Execute the real handler against a fake Kong PDK/auth service. No credentials or network.
local response, calls, request, sent, cleared, outgoing, result
package.preload['resty.http'] = function()
  return {new=function() return {
    set_timeouts=function() end,
    request_uri=function(self,url,opts) calls=calls+1; sent=opts; assert(opts.method=='GET'); assert(opts.body==nil); return response end
  } end}
end
ngx={escape_uri=function(s) return (s:gsub('[^%w%-_.~]',function(c)return string.format('%%%02X',string.byte(c))end)) end}
local handler=dofile('plugins/sso/kong/plugins/session-auth/handler.lua')
local conf={hosts={'app.example.test'}, mode='browser',email_domain='example.test',https_port=443,api_paths={'/api','/upload'}}
local function setup()
  response={status=202,headers={['X-Auth-Request-User']='user',['X-Auth-Request-Email']='user@example.test'}}
  calls=0; sent=nil; cleared={}; outgoing={}; result=nil
  request={host='app.example.test',scheme='https',method='GET',path='/get',cookie='__Host-infra_sso=session; app=value'}
  kong={ctx={plugin={}},request={
    get_host=function()return request.host end,get_scheme=function()return request.scheme end,
    get_method=function()return request.method end,get_path=function()return request.path end,
    get_path_with_query=function()return request.path..'?q=1' end,
    get_header=function(n) if n=='Cookie' then return request.cookie end; return nil end
  },service={request={clear_header=function(n)cleared[n]=true end,set_header=function(n,v)outgoing[n]=v end}},
  response={exit=function(s,b,h)result={status=s,headers=h};return result end,
    get_header=function()return nil end,set_header=function(n,v)outgoing[n]=v end}}
  conf.mode='browser'
end
local count=0
local function test(name,fn) setup();fn();count=count+1;print('PASS '..name) end
test('authenticated bodyless check and isolated cookies',function()
  handler:access(conf);assert(result==nil and calls==1)
  assert(sent.headers.Cookie=='__Host-infra_sso=session');assert(outgoing.Cookie==' app=value')
  assert(outgoing['X-Auth-Request-Email']=='user@example.test');assert(cleared['X-Forwarded-User'])
end)
test('browser redirect preserves URL',function()
 response.status=401;handler:access(conf);assert(result.status==302);assert(result.headers.Location:find('https%%3A%%2F%%2Fapp'))
end)
for _,method in ipairs({'POST','PUT','PATCH','DELETE'})do
test('unsafe '..method..' is never replayed',function()request.method=method;response.status=401;handler:access(conf);assert(result.status==401)end)
end
test('api prefix GET receives 401',function()request.path='/api/items';response.status=401;handler:access(conf);assert(result.status==401)end)
test('api mode receives 401',function()conf.mode='api';response.status=401;handler:access(conf);assert(result.status==401)end)
test('auth outage fails closed',function()response=nil;handler:access(conf);assert(result.status==503)end)
test('unexpected auth response fails closed',function()response.status=302;handler:access(conf);assert(result.status==503)end)
test('missing identity fails closed',function()response.headers={};handler:access(conf);assert(result.status==503)end)
test('foreign email domain refused',function()response.headers['X-Auth-Request-Email']='user@other.test';handler:access(conf);assert(result.status==403)end)
test('auth forbidden stays forbidden',function()response.status=403;handler:access(conf);assert(result.status==403)end)
test('unknown host refused before auth',function()request.host='evil.test';handler:access(conf);assert(result.status==403 and calls==0)end)
test('plain HTTP refused',function()request.scheme='http';handler:access(conf);assert(result.status==403 and calls==0)end)
test('refresh split cookies without upstream cookie',function()
 response.headers['Set-Cookie']={'a=1','b=2'};handler:access(conf);handler:header_filter(conf);assert(#outgoing['Set-Cookie']==2)
end)
test('refresh preserves application cookies',function()
 response.headers['Set-Cookie']='session=new';handler:access(conf)
 kong.response.get_header=function()return 'app=old'end;handler:header_filter(conf);assert(#outgoing['Set-Cookie']==2)
end)
test('public route strips spoofed identity but does not authenticate',function()conf.mode='public';handler:access(conf);assert(calls==0 and result==nil and cleared['X-Auth-Request-Email'])end)
test('oauth callbacks are the only public auth endpoints',function()conf.mode='oauth';request.path='/oauth2/auth';handler:access(conf);assert(result.status==404)end)
test('oauth callback forwards trusted host only',function()conf.mode='oauth';request.path='/oauth2/callback';handler:access(conf);assert(result==nil and calls==0);assert(outgoing['X-Forwarded-Host']==request.host)end)
print('Lua handler tests: '..count..' passed')
