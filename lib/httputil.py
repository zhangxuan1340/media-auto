"""共享 HTTP 工具: 不走系统代理的 opener。

⚠️ 与 clouddrive 客户端 _clean_env、tmdb 客户端 trust_env=False 同一类坑:
机器上配了系统 http_proxy/https_proxy/all_proxy 时, 内网地址
(172.16.x.x / your-site.example.com 等)会被代理拦截 → 502/连不上。
scripts/ 下的 urllib 调用统一走这里, 别再各写各的。
"""
import urllib.request


def no_proxy_opener():
    """返回一个禁用系统代理的 opener(ProxyHandler({}) 清空全部代理)。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))
