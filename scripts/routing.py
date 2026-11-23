"""Render real NGINX weighted, sticky, and read-only mirrored traffic routes."""

import hashlib
import re
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
SLOTS = ("blue", "green", "candidate", "rolling")


def workload(slot, image, version):
    if slot not in SLOTS:
        raise ValueError(f"Unknown deployment slot: {slot}")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/@:-]+", image):
        raise ValueError("Image must be a registry image reference without whitespace.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]*", version):
        raise ValueError("Invalid application version.")
    return Template((ROOT / "k8s" / "workload.yaml").read_text()).substitute(
        SLOT=slot, IMAGE=image, VERSION=version
    )


def nginx_config(canary=0):
    if (
        isinstance(canary, bool)
        or not isinstance(canary, int)
        or not 0 <= canary <= 100
    ):
        raise ValueError("Canary percentage must be an integer from 0 to 100.")
    if canary == 100:
        split = "* aceest-candidate.aceest.svc.cluster.local;"
    elif canary == 0:
        split = "* aceest-stable.aceest.svc.cluster.local;"
    else:
        split = (
            f"{canary}% aceest-candidate.aceest.svc.cluster.local;\n"
            "    * aceest-stable.aceest.svc.cluster.local;"
        )
    return f"""
resolver kube-dns.kube-system.svc.cluster.local valid=5s ipv6=off;
split_clients "${{request_id}}" $canary_backend {{
    {split}
}}
split_clients "${{remote_addr}}${{http_user_agent}}" $assigned_variant {{
    50% B;
    * A;
}}
map $cookie_aceest_variant $variant {{
    A A;
    B B;
    default $assigned_variant;
}}
map $variant $ab_backend {{
    A aceest-stable.aceest.svc.cluster.local;
    B aceest-candidate.aceest.svc.cluster.local;
}}
map $request_method $mirror_allowed {{
    GET 1;
    HEAD 1;
    default 0;
}}
map $request_uri $shadow_uri {{
    ~^/shadow/(.*)$ /$1;
    default /;
}}
log_format release '$remote_addr "$request" $status upstream=$upstream_addr '
                   'canary=$canary_backend variant=$variant';
server {{
    listen 8080;
    access_log /dev/stdout release;
    log_subrequest on;
    proxy_connect_timeout 3s;
    proxy_read_timeout 35s;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    location = /router-health {{ return 200 "ready\\n"; }}
    location /rolling/ {{
        set $backend aceest-rolling.aceest.svc.cluster.local;
        rewrite ^/rolling/(.*)$ /$1 break;
        proxy_pass http://$backend:5000;
    }}
    location /blue-green/ {{
        set $backend aceest-stable.aceest.svc.cluster.local;
        rewrite ^/blue-green/(.*)$ /$1 break;
        proxy_pass http://$backend:5000;
    }}
    location /canary/ {{
        rewrite ^/canary/(.*)$ /$1 break;
        proxy_pass http://$canary_backend:5000;
    }}
    location /ab/ {{
        add_header Set-Cookie
            "aceest_variant=$variant; Path=/; HttpOnly; SameSite=Lax" always;
        add_header X-ACEest-Variant $variant always;
        rewrite ^/ab/(.*)$ /$1 break;
        proxy_pass http://$ab_backend:5000;
    }}
    location /shadow/ {{
        mirror /_mirror;
        mirror_request_body off;
        set $backend aceest-stable.aceest.svc.cluster.local;
        rewrite ^/shadow/(.*)$ /$1 break;
        proxy_pass http://$backend:5000;
    }}
    location = /_mirror {{
        internal;
        if ($mirror_allowed = 0) {{ return 204; }}
        set $backend aceest-candidate.aceest.svc.cluster.local;
        proxy_pass http://$backend:5000$shadow_uri;
    }}
    location / {{
        set $backend aceest-stable.aceest.svc.cluster.local;
        proxy_pass http://$backend:5000;
    }}
}}
"""


def router_resources(canary=0):
    config = nginx_config(canary)
    name = "aceest-routing-" + hashlib.sha256(config.encode()).hexdigest()[:12]
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "aceest"},
        "immutable": True,
        "data": {"default.conf": config},
    }
    deployment = Template((ROOT / "k8s" / "router.yaml").read_text()).substitute(
        CONFIG_NAME=name
    )
    return configmap, deployment
