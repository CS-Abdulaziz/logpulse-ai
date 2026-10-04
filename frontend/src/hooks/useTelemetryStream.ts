'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { LogLevel } from '@/lib/types'

const MAX_LOGS = 500

export interface TelemetryLogEntry {
  id: string
  timestamp: string
  level: LogLevel
  message: string
  service?: string
  raw: string
}

export interface DetectionState {
  id: string
  title: string
  summary: string
  confidence: number
  impact_area: string
  status: 'critical' | 'warning' | 'healthy'
  signature_id: string
  signature_label: string
  trigger_log: string
  scenario: string
  detected_at: number
}

export interface HeatmapCell {
  id: string
  row: number
  col: number
  intensity: number
  state: 'normal' | 'warning' | 'critical'
}

export interface SignatureItem {
  id: string
  label: string
  minutesAgo: number
}

export interface ErrorConcentrationItem {
  service: string
  percent: number
}

interface SnapshotPayload {
  logs?: TelemetryLogEntry[]
  detection?: DetectionState | null
  signatures?: SignatureItem[]
  error_concentration?: ErrorConcentrationItem[]
  heatmap?: HeatmapCell[]
}

export interface TelemetryStreamState {
  logs: TelemetryLogEntry[]
  detection: DetectionState | null
  heatmap: HeatmapCell[]
  signatures: SignatureItem[]
  errorConcentration: ErrorConcentrationItem[]
  connected: boolean
  complete: boolean
  error: string | null
}

function safeParse<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export function useTelemetryStream(scenario = 'oom_critical'): TelemetryStreamState {
  const [logs, setLogs] = useState<TelemetryLogEntry[]>([])
  const [detection, setDetection] = useState<DetectionState | null>(null)
  const [heatmap, setHeatmap] = useState<HeatmapCell[]>([])
  const [signatures, setSignatures] = useState<SignatureItem[]>([])
  const [errorConcentration, setErrorConcentration] = useState<ErrorConcentrationItem[]>([])
  const [connected, setConnected] = useState(false)
  const [complete, setComplete] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const appendLog = useCallback((entry: TelemetryLogEntry) => {
    setLogs((prev) => [...prev, entry].slice(-MAX_LOGS))
  }, [])

  useEffect(() => {
    const source = new EventSource(`/api/telemetry/stream?scenario=${encodeURIComponent(scenario)}`)

    source.addEventListener('open', () => {
      setConnected(true)
      setError(null)
    })

    source.addEventListener('snapshot', (e: MessageEvent) => {
      const payload = safeParse<SnapshotPayload>(e.data)
      if (!payload) return
      setLogs((payload.logs ?? []).slice(-MAX_LOGS))
      setDetection(payload.detection ?? null)
      setSignatures(payload.signatures ?? [])
      setErrorConcentration(payload.error_concentration ?? [])
      setHeatmap(payload.heatmap ?? [])
    })

    source.addEventListener('log', (e: MessageEvent) => {
      const payload = safeParse<TelemetryLogEntry>(e.data)
      if (payload) appendLog(payload)
    })

    source.addEventListener('detection_state', (e: MessageEvent) => {
      const payload = safeParse<DetectionState>(e.data)
      if (payload) setDetection(payload)
    })

    source.addEventListener('heatmap', (e: MessageEvent) => {
      const payload = safeParse<{ cells: HeatmapCell[] }>(e.data)
      if (payload?.cells) setHeatmap(payload.cells)
    })

    source.addEventListener('error_concentration', (e: MessageEvent) => {
      const payload = safeParse<{ items: ErrorConcentrationItem[] }>(e.data)
      if (payload?.items) setErrorConcentration(payload.items)
    })

    source.addEventListener('signature', (e: MessageEvent) => {
      const payload = safeParse<SignatureItem>(e.data)
      if (!payload) return
      setSignatures((prev) => {
        const deduped = prev.filter((item) => item.id !== payload.id)
        return [payload, ...deduped].slice(0, 20)
      })
    })

    source.addEventListener('complete', () => {
      setComplete(true)
      source.close()
    })

    source.addEventListener('telemetry_error', (e: MessageEvent) => {
      const payload = safeParse<{ message?: string }>(e.data)
      setError(payload?.message ?? 'Telemetry stream error')
    })

    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        setConnected(false)
      }
    }

    return () => {
      source.close()
      setConnected(false)
    }
  }, [appendLog, scenario])

  return useMemo(() => ({
    logs,
    detection,
    heatmap,
    signatures,
    errorConcentration,
    connected,
    complete,
    error,
  }), [logs, detection, heatmap, signatures, errorConcentration, connected, complete, error])
}
