// 导入原生模块
const nativeBinding = require('./target/nodejs/index.js');

/**
 * 初始化网络模块
 * @param {InitConfig} initConfig - 初始化配置对象
 * @param {Function} [ttnetReportFn] - 可选的报告回调函数
 * @returns {void}
 */
function init(initConfig, ttnetReportFn) {
  return nativeBinding.init(initConfig, ttnetReportFn);
}

/**
 * 符合Node.js标准的fetch实现
 * @param {RequestInfo} input 请求URL或Request对象
 * @param {RequestInit} [init] 请求配置选项
 * @returns {Promise<Response>}
 * @note 已知的与Node.js原生fetch的差异：
 * 1. 响应头：会额外添加一些响应头字段，包括但不限于：
 *  - x-net-info.remoteaddr: 远端服务器地址
 *  - x-request-id: 请求唯一标记，每次fetch调用都不同
 * 2. 暂不支持window选项
 * 3. 暂不支持referrerPolicy选项
 * 4. ……
 */
async function fetch(input, init) {
  // 解析请求参数
  const request = input instanceof Request ? input : new Request(input, init);
  const requestOptions = {
    url: request.url,
    method: request.method,
    headers: Array.from(request.headers.entries()),
    // body: request.body, 通过bodyProviderPair处理
    mode: request.mode,
    credentials: request.credentials,
    cache: request.cache,
    redirect: request.redirect,
    referrer: request.referrer,
    // referrerPolicy: request.referrerPolicy, // todo
    integrity: request.integrity,
    keepalive: request.keepalive,
    // signal: request.signal, 通过AbortSignal处理
    // window: request.window   // todo
  };

  let data_receiver = null;
  if (request.body) {
    const [tx, rx] = nativeBinding.bodyProviderPair();
    data_receiver = rx;
    const reader = request.body.getReader();
    (async () => {
      try {
        while (true) {
          if (request.signal.aborted) {
            await tx.sendError(String('canceled by abort_signal on request'));
          }

          const { done, value } = await reader.read();
          
          if (done) {
            await tx.complete();
            break;
          }
          await tx.sendData(Buffer.from(value));
        }
      } catch (e) {
        await tx.sendError(String(e));
      }
    })();
  }

  const abort_signal = new nativeBinding.AbortSignal();
  const abort_handler = () => abort_signal.abort();
  request.signal.addEventListener('abort', abort_handler);

  try {
    // 调用原生rawFetch方法
    const [rawResponse, body] = await nativeBinding.rawFetch(requestOptions, data_receiver, abort_signal);

    // 转换为标准Response对象
    const responseStream = body.toReadableStream();
    const reader = responseStream.getReader();
    const wrappedStream = new ReadableStream({
      async pull(controller) {
        try {
          const { done, value } = await reader.read();
          if (done) {
            request.signal.removeEventListener('abort', abort_handler);
            controller.close();
            return;
          }
          controller.enqueue(value);
        } catch (error) {
          request.signal.removeEventListener('abort', abort_handler);
          if (request.signal.aborted) {
            controller.error(request.signal.reason);
          } else {
            controller.error(error);
          }
        }
      },
      cancel(reason) {
        request.signal.removeEventListener('abort', abort_handler);
        return reader.cancel(reason);
      }
    });

    return new Response(wrappedStream, {
      status: rawResponse.status,
      statusText: rawResponse.statusText,
      headers: new Headers(rawResponse.headers),
      ok: rawResponse.ok,
      url: rawResponse.url,
      redirected: rawResponse.redirected,
      type: rawResponse.type
    });
  } catch (error) {
    request.signal.removeEventListener('abort', abort_handler);
    if (request.signal.aborted) {
      throw request.signal.reason;
    } else {
      throw error;
    }
  }
}

/**
 * 设置请求配置
 * @param {String} config - json格式的字符串
 * @returns {void}
 */
function setCloudConfig(config) {
  return nativeBinding.setCloudConfig(config)
}

module.exports = {
  init,
  fetch,
  setCloudConfig
};