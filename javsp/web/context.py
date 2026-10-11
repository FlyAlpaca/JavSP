"""每个站点任务独立的配置、请求会话及动态状态。"""

from javsp.web.base import Request

PERMANENT_URLS = {
    'javbus': 'https://www.javbus.com', 'javdb': 'https://javdb.com',
    'javlib': 'https://www.javlibrary.com', 'avsox': 'https://avsox.click',
}


class SiteContext:
    def __init__(self, cfg, site):
        self.cfg, self.site = cfg, site
        self.request = Request(cfg.network, use_scraper=site in {'javdb', 'javlib'})
        permanent = PERMANENT_URLS.get(site, '')
        self.base_url = permanent if cfg.network.proxy_server else str(cfg.network.proxy_free.get(site, permanent))
        if site == 'avsox':
            self.base_url = str(cfg.network.proxy_free[site])
        if site == 'javlib':
            self.base_url = ''
        self.cookies_pool = None
        if site == 'javdb':
            self.request.headers['Accept-Language'] = 'zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.7,en;q=0.6,ja;q=0.5'
        if site == 'fanza':
            self.request.cookies = {'age_check_done': '1'}
        if site == 'fanza':
            self.request.headers['Accept-Language'] = 'ja,en-US;q=0.9'
        if site == 'mgstage':
            self.request.cookies = {'adc': '1'}

    def reset_request(self):
        headers = self.request.headers.copy()
        self.request.close()
        self.request = Request(self.cfg.network, use_scraper=self.site in {'javdb', 'javlib'})
        self.request.headers.update(headers)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.request.close()
