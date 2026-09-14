/**
 * 账号池变化通知
 *
 * 场景：添加账号时拉起的登录脚本是**后台异步**的 —— 用户可能在浏览器里
 * 填密码填两分钟，脚本才把新账号写进 config.json。此时账号列表页已经渲染完了，
 * 它不会自己知道「刚刚多了个账号」。
 *
 * v2.3 的 GUI 没有这个问题：登录脚本在 GUI 自己的线程里跑，跑完直接调
 * `refresh_account_list()`。v3.0 是「界面进程 ↔ 网关进程」分离的，
 * 只能靠一个事件把这个事实传过去。
 *
 * 为什么不做成轮询网关：为了一个低频事件让账号列表每几秒发一次请求不划算，
 * 而脚本结束的时机我们**是知道的**（跟随输出时看到进程退出），推一下即可。
 */
import type { Channel } from '@/types/domain'

type Listener = (channel: Channel) => void

const listeners = new Set<Listener>()

/** 订阅账号池变化；返回取消订阅函数（务必在 useEffect 的清理里调用） */
export function onAccountsChanged(fn: Listener): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** 通知「某通道的账号池可能变了」（登录脚本结束、账号被外部写入等） */
export function emitAccountsChanged(channel: Channel): void {
  listeners.forEach((fn) => {
    try {
      fn(channel)
    } catch (e) {
      // 单个订阅者抛错不该影响其它订阅者（也不该把异常抛回调用它的数据层）
      console.warn('[open-ai] onAccountsChanged 订阅者异常：', e)
    }
  })
}
