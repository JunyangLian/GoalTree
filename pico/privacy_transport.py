"""Explicit transport hooks. No global monkeypatch or TLS interception."""
import urllib.request
from .privacy import CURRENT, outbound, loopback


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from .privacy import PrivacyDenied
        raise PrivacyDenied('redirect_requires_new_policy_check')


def open_checked(request, *, timeout, route='environment', purpose='model'):
    if not CURRENT.get():
        opener = urllib.request.urlopen if route == 'environment' else urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        return opener(request,timeout=timeout)
    outbound(request.full_url,request.data or '',dict(request.header_items()),purpose,
             credential_headers=('authorization','context7_api_key','x-api-key'))
    # A local clearance must not send core data through an inherited HTTP proxy.
    if loopback(request.full_url):route='direct'
    handlers = [NoRedirect()]
    if route == 'direct':handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(request,timeout=timeout)
