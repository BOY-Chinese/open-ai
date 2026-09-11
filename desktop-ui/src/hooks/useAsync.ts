import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AsyncStatus } from '@/types/domain'

/**
 * useAsync — 统一异步数据 hook
 *
 * 职责：
 *  - 暴露 status/data/error，页面据此渲染 Skeleton / 空态 / 错误态
 *  - 提供 reload / setData（乐观更新用）
 *  - 防止卸载后 setState（内存泄漏 / 警告）
 *
 * 所有页面数据获取必须经由此 hook，禁止在组件内裸用 useEffect+useState。
 */
export function useAsync<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  initial: T
): {
  status: AsyncStatus
  data: T
  error?: string
  loading: boolean
  reload: () => Promise<void>
  setData: React.Dispatch<React.SetStateAction<T>>
} {
  const [status, setStatus] = useState<AsyncStatus>('idle')
  const [data, setData] = useState<T>(initial)
  const [error, setError] = useState<string | undefined>()

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // fetcher 每次渲染都是新引用，用 ref 持有最新值，避免把它塞进依赖
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const reload = useCallback(async () => {
    setStatus('loading')
    setError(undefined)
    try {
      const result = await fetcherRef.current()
      if (!mounted.current) return
      setData(result)
      setStatus('success')
    } catch (e) {
      if (!mounted.current) return
      setError(e instanceof Error ? e.message : String(e))
      setStatus('error')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return {
    status,
    data,
    error,
    loading: status === 'loading' || status === 'idle',
    reload,
    setData,
  }
}

/** 多选集合管理：切换 / 全选 / 反选 / 清空 */
export function useSelection(ids: string[]) {
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // 数据变化时剔除已不存在的 id
  const idSet = useMemo(() => new Set(ids), [ids])
  useEffect(() => {
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => idSet.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [idSet])

  const toggle = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const toggleAll = useCallback(() => {
    setSelected((prev) => (prev.size === ids.length ? new Set() : new Set(ids)))
  }, [ids])

  const clear = useCallback(() => setSelected(new Set()), [])

  const allChecked = ids.length > 0 && selected.size === ids.length
  const someChecked = selected.size > 0 && selected.size < ids.length

  return {
    selected,
    toggle,
    toggleAll,
    clear,
    allChecked,
    someChecked,
    count: selected.size,
    has: (id: string) => selected.has(id),
  }
}
