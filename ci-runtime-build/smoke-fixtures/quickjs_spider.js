// QuickJS ES module, NOT a Node plugin. http() is the app's owned network bridge.
let base;
async function read() {
    const response = await http(base + '/catalog.json', {timeout: 5000, callTimeout: 5000});
    if (response.error || response.code !== 200) throw new Error('Local JSON request failed: ' + (response.error || response.code));
    const data = JSON.parse(response.content);
    if (data.fixture !== 'tv-runtime-local-v1') throw new Error('Unexpected fixture JSON');
    return data;
}
function page(items, pg) {
    return {list: String(pg) === '1' ? items : [], page: Number(pg), pagecount: 1, limit: 20, total: items.length};
}
export default {
    init(settings) {
        if (typeof settings === 'string') settings = JSON.parse(settings);
        base = settings.base.replace(/\/$/, '');
        if (!/^http:\/\/(127\.0\.0\.1|localhost):[0-9]+$/.test(base)) throw new Error('Fixture base must be local HTTP over adb reverse');
    },
    async home(filter) {
        const data = await read();
        return {class: data.class, list: data.list};
    },
    async homeVod() { return {list: (await read()).list}; },
    async category(tid, pg, filter, extend) { return page((await read()).list.filter(item => item.type_id === tid), pg); },
    // The QuickJS bridge passes ids.get(0), not the Python List[str].
    async detail(id) { return {list: (await read()).list.filter(item => item.vod_id === id)}; },
    async search(key, quick, pg = '1') { return page((await read()).list.filter(item => item.vod_name.toLowerCase().includes(key.toLowerCase())), pg); },
    async play(flag, id, flags) {
        const data = await read();
        if (flag !== 'local' || !data.list.some(item => item.vod_id === id)) throw new Error('Unknown local episode');
        if (!data.media.startsWith('/media/')) throw new Error('Media must be a local fixture route');
        return {parse: 0, url: base + data.media, header: {}, subtitles: data.subtitles.map(item => ({...item, url: base + item.url}))};
    },
    sniffer() { return false; },
    isVideo(url) { return url.startsWith(base + '/media/'); },
    destroy() {}
};
