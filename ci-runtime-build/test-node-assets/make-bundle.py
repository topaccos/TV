#!/usr/bin/env python3
"""Generate the four-file NodeBundle consumed by Android NodeRuntime (NOT a ZIP).

Example: uv run python make-bundle.py --base http://127.0.0.1:18765 \
    --output /path/to/http-root/node-bundle
The base must be reachable FROM ANDROID, not just from the test driver.
Output directory must be new; all files are generated atomically as a directory.
No Node interpreter or device access is used by this generator.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import tempfile
from urllib.parse import urlsplit

INDEX = r'''"use strict";
const http = require("http");
const https = require("https");
function getJson(url) {
  return new Promise((resolve, reject) => {
    const request = (url.protocol === "https:" ? https : http).get(url, res => {
      if (res.statusCode !== 200) { res.resume(); reject(new Error("fixture HTTP " + res.statusCode)); return; }
      let text = "";
      res.setEncoding("utf8");
      res.on("data", part => { text += part; if (text.length > 1048576) res.destroy(new Error("fixture too large")); });
      res.on("error", reject);
      res.on("end", () => { try { resolve(JSON.parse(text)); } catch (error) { reject(error); } });
    });
    request.setTimeout(10000, () => request.destroy(new Error("fixture timeout")));
    request.on("error", reject);
  });
}
module.exports = {start(settings) {
  const base = settings.base;
  const route = "/spider/local";
  let initialized = false;
  async function catalog(method) {
    const url = new URL(base + "/catalog.json");
    url.searchParams.set("node_probe", settings.nonce);
    url.searchParams.set("method", method);
    url.searchParams.set("pid", String(process.pid));
    url.searchParams.set("version", process.version);
    const value = await getJson(url);
    if (value.fixture !== "tv-runtime-local-v1") throw new Error("wrong fixture catalog");
    return value;
  }
  function page(list, pg) {
    const number = Number(pg || 1);
    if (!Number.isInteger(number) || number < 1) throw new Error("bad page");
    return {list: number === 1 ? list : [], page: number, pagecount: 1, limit: 20, total: list.length};
  }
  const actions = {
    async init(body) {
      const ext = JSON.parse(body.ext || body.extend || "{}");
      if (ext.base !== base || ext.nonce !== settings.nonce) throw new Error("ext mismatch");
      await catalog("init"); initialized = true; return {};
    },
    async home(body) { const data = await catalog("home"); return {class: data.class, list: data.list}; },
    async homeVod(body) { return {list: (await catalog("homeVod")).list}; },
    async category(body) { const data = await catalog("category"); return page(data.list.filter(item => item.type_id === body.id), body.page); },
    async detail(body) { return {list: (await catalog("detail")).list.filter(item => item.vod_id === body.id)}; },
    async search(body) { const data = await catalog("search"); return page(data.list.filter(item => item.vod_name.toLowerCase().includes(String(body.wd || "").toLowerCase())), body.page); },
    async play(body) {
      const data = await catalog("play");
      if (body.flag !== "local" || !data.list.some(item => item.vod_id === body.id)) throw new Error("unknown local episode");
      return {parse: 0, url: base + data.media, header: {}, subtitles: (data.subtitles || []).map(item => ({...item, url: base + item.url}))};
    }
  };
  global.catServerFactory(async (req, res) => {
    function reply(code, value) { res.writeHead(code, {"Content-Type": "application/json", "Cache-Control": "no-store"}); res.end(JSON.stringify(value)); }
    try {
      const path = new URL(req.url, "http://127.0.0.1").pathname;
      if (req.method === "GET" && path === "/config") {
        await catalog("config");
        return reply(200, {video: {sites: [{key: "node_local", name: "Node local " + settings.nonce, api: route, type: 3, searchable: 1, quickSearch: 1, filterable: 1, ext: JSON.stringify(settings)}]}});
      }
      const name = path.startsWith(route + "/") ? path.slice(route.length + 1) : "";
      if (req.method !== "POST" || !Object.prototype.hasOwnProperty.call(actions, name)) { req.resume(); return reply(404, {error: "not found"}); }
      if (String(req.headers["content-type"] || "").split(";")[0].trim() !== "application/json") { req.resume(); return reply(415, {error: "JSON required"}); }
      let text = "";
      for await (const part of req) { text += part; if (text.length > 65536) throw new Error("body too large"); }
      const body = JSON.parse(text || "{}");
      if (name !== "init" && !initialized) throw new Error("spider not initialized");
      return reply(200, await actions[name](body));
    } catch (error) { return reply(400, {error: String(error.message || error)}); }
  }).listen(0);
}};
'''


def generate(base, output, nonce=None):
    base = base.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("base must be an HTTP(S) fixture URL without query, fragment or credentials")
    nonce = nonce or secrets.token_hex(8)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("refusing to overwrite existing bundle: " + str(output))
    settings = {"base": base, "nonce": nonce}
    files = {"index.js": INDEX.encode("utf-8"), "index.config.js": ("module.exports = " + json.dumps(settings, ensure_ascii=True) + ";\n").encode("utf-8")}
    for name, payload in list(files.items()):
        files[name + ".md5"] = (hashlib.md5(payload).hexdigest() + "\n").encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".node-bundle-", dir=output.parent))
    try:
        for name, payload in files.items():
            (temporary / name).write_bytes(payload)
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"bundle": str(output), "entry": "index.js.md5", "site_key": "node_local", "api": "node:/spider/local", "nonce": nonce, "android_runtime_tested": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nonce")
    args = parser.parse_args()
    print(json.dumps(generate(args.base, args.output, args.nonce), ensure_ascii=False))
