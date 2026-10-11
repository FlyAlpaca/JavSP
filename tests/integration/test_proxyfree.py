from javsp.config import load_config
from javsp.web.context import SiteContext
from javsp.web.proxyfree import get_proxy_free_url


def test_get_url():
    for site in ("javlib", "javdb"):
        with SiteContext(load_config(), site) as context:
            assert get_proxy_free_url(site, context=context) != ""


def test_get_url_with_prefer():
    prefer_url = "https://www.baidu.com"
    with SiteContext(load_config(), "javlib") as context:
        assert prefer_url == get_proxy_free_url("javlib", prefer_url, context=context)
