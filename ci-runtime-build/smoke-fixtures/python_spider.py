"""Owned Python Spider. All data requests use the app-bound net adapter."""
import json
from urllib.parse import urlsplit
from base.spider import Spider as BaseSpider


class Spider(BaseSpider):
    def init(self, extend=""):
        settings = json.loads(extend) if isinstance(extend, str) else extend
        self.base = settings["base"].rstrip("/")
        target = urlsplit(self.base)
        if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("Fixture base must be local HTTP over adb reverse")

    def getName(self):
        return "Local Python smoke"

    def _read(self):
        response = self.net.req(self.base + "/catalog.json", {"timeout": 5000, "callTimeout": 5000})
        if response.get("error") or response.get("code") != 200:
            raise RuntimeError("Local JSON request failed: " + str(response.get("error", response.get("code"))))
        data = json.loads(response["content"])
        if data.get("fixture") != "tv-runtime-local-v1":
            raise ValueError("Unexpected fixture JSON")
        return data

    def homeContent(self, filter):
        data = self._read()
        return {"class": data["class"], "list": data["list"]}

    def homeVideoContent(self):
        return {"list": self._read()["list"]}

    def categoryContent(self, tid, pg, filter, extend):
        items = [item for item in self._read()["list"] if item["type_id"] == tid]
        return {"list": items if str(pg) == "1" else [], "page": int(pg), "pagecount": 1, "limit": 20, "total": len(items)}

    def detailContent(self, ids):
        return {"list": [item for item in self._read()["list"] if item["vod_id"] in ids]}

    def searchContent(self, key, quick, pg="1"):
        items = [item for item in self._read()["list"] if key.lower() in item["vod_name"].lower()]
        return {"list": items if str(pg) == "1" else [], "page": int(pg), "pagecount": 1, "limit": 20, "total": len(items)}

    def playerContent(self, flag, id, vipFlags):
        data = self._read()
        if flag != "local" or id not in [item["vod_id"] for item in data["list"]]:
            raise ValueError("Unknown local episode")
        if not data["media"].startswith("/media/"):
            raise ValueError("Media must be a local fixture route")
        return {"parse": 0, "url": self.base + data["media"], "header": {},
                "subtitles": [{**item, "url": self.base + item["url"]} for item in data["subtitles"]]}

    def isVideoFormat(self, url):
        return url.startswith(self.base + "/media/")

    def manualVideoCheck(self):
        return False
