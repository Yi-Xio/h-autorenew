// Hax 的两处广告检测会清空页面。仅处理已核实的探测请求和拦截回调。
// 规则依据：https://github.com/AdguardTeam/AdguardFilters/issues/166266
(() => {
    "use strict";
    if (location.origin !== "https://hax.co.id") return;

    const marker = Symbol.for("h-autorenew.hax-rules.v1");
    const previous = window[marker];
    if (previous?.fetch === window.fetch &&
        previous?.listener === EventTarget.prototype.addEventListener) return;

    const RequestType = window.Request;
    const ResponseType = window.Response;
    const URLType = window.URL;
    const probeURL = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js";
    const wrapped = new Proxy(window.fetch, {
        apply(target, context, args) {
            const input = args[0];
            const isRequest = input instanceof RequestType;
            const address = isRequest ? input.url : input instanceof URLType ? input.href : input;
            if (address !== probeURL) return Reflect.apply(target, context, args);

            let request = null;
            try {
                const init = args[1];
                const method = init?.method ?? (isRequest ? input.method : "GET");
                if (typeof method === "string" && method.toUpperCase() === "GET" &&
                    init?.body == null && (!isRequest || input.body === null)) {
                    // 交给浏览器校验请求选项，不吞掉无效输入或已取消请求。
                    request = new RequestType(input, init);
                }
            } catch (_) {
                // 无效选项仍交由原始 fetch 返回标准错误。
            }
            if (request === null) return Reflect.apply(target, context, args);
            if (request.signal.aborted) return Promise.reject(request.signal.reason);
            return Promise.resolve(new ResponseType("{}", {
                status: 200,
                headers: {"Content-Type": "application/json"},
            }));
        },
    });
    const toString = Function.prototype.toString;
    const listener = new Proxy(EventTarget.prototype.addEventListener, {
        apply(target, context, args) {
            if (args[0] === "load" && typeof args[1] === "function" &&
                toString.call(args[1]).includes("Please disable AdBlock to access this site!")) {
                return undefined;
            }
            return Reflect.apply(target, context, args);
        },
    });
    window.fetch = wrapped;
    EventTarget.prototype.addEventListener = listener;
    Object.defineProperty(window, marker, {
        value: {fetch: wrapped, listener},
        configurable: true,
    });
})();
