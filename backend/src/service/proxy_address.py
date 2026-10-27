"""Resolve client addresses only through explicitly trusted private proxy peers."""
import ipaddress
import socket


def client_address(request, settings) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-real-ip")
    if not forwarded or not settings.trusted_proxy_hosts:
        return peer
    try:
        address = str(ipaddress.ip_address(forwarded))
        peer_address = str(ipaddress.ip_address(peer))
    except ValueError:
        return peer
    for host in settings.trusted_proxy_hosts:
        try:
            trusted = {str(ipaddress.ip_address(item[4][0])) for item in
                socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            continue
        if peer_address in trusted:
            return address
    return peer
