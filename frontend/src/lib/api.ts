/**
 * src/lib/api.ts — LogPulse AI API client
 *
 * Flip USE_MOCK to false to connect to the real FastAPI backend.
 * All functions auto-switch between mock data and live API.
 */

// ── Toggle ────────────────────────────────────────────────────────────────────
const USE_MOCK = false

// ── Types (mirrors api/models/schemas.py) ─────────────────────────────────────
export type IncidentStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'executing'
  | 'completed'
  | 'failed'
  | 'rejected'

export interface Incident {
  id:             string
  status:         IncidentStatus
  created_at:     string
  input_log:      string
  classification: Record<string, unknown> | null
  rag:            Record<string, unknown> | null
  diagnosis:      Record<string, unknown> | null
  solution:       Record<string, unknown> | null
  risk:           Record<string, unknown> | null
  execution:      Record<string, unknown> | null
  report_path:    string | null
  duration_ms:    number | null
}

export interface StageEvent {
  name:        string
  status:      'running' | 'done' | 'error'
  result?:     Record<string, unknown>
  duration_ms?: number
  error?:      string
}

export interface AwaitingApprovalEvent {
  commands:    string[]
  risk_level:  string
  incident_id: string
}

export interface StatsData {
  total_incidents:   number
  avg_resolution_ms: number
  ai_accuracy:       number
  uptime_percent:    number
  by_category:       Record<string, number>
  by_severity:       Record<string, number>
  by_outcome:        Record<string, number>
  top_playbooks:     Array<{ name: string; count?: number }>
  recent_incidents:  Array<Record<string, unknown>>
}

export interface PlaybookItem {
  id:               string
  title:            string
  category:         string
  severity_typical: string | null
  resolution_steps: string[]
  code_fix:         string | null
  source:           string | null
}

// ── Base URL ──────────────────────────────────────────────────────────────────
const BASE = '/api'

async function _get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`)
  return res.json() as Promise<T>
}

async function _post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText)
    throw new Error(`POST ${path} → ${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

// ── Mock import (lazy, only used when USE_MOCK=true) ──────────────────────────
async function _mockStats(): Promise<StatsData> {
  const { clusterMetrics, topPlaybooks } = await import('./mock-data')
  return {
    total_incidents:   clusterMetrics.totalIncidents,
    avg_resolution_ms: clusterMetrics.avgResolutionSeconds * 1000,
    ai_accuracy:       clusterMetrics.aiAccuracy,
    uptime_percent:    clusterMetrics.uptimePercent,
    by_category:       { Memory: 120, Security: 55, Database: 42, Network: 30 },
    by_severity:       { CRITICAL: 89, Warning: 103, Info: 55 },
    by_outcome:        { completed: 200, rejected: 30, failed: 17 },
    top_playbooks:     topPlaybooks.map((p) => ({ name: p.name, count: p.successRate })),
    recent_incidents:  [],
  }
}

// ── Public API ────────────────────────────────────────────────────────────────

export async function submitIncident(log: string, scenario = 'default'): Promise<Incident> {
  if (USE_MOCK) {
    return {
      id: 'mock-' + Math.random().toString(36).slice(2),
      status: 'queued',
      created_at: new Date().toISOString(),
      input_log: log,
      classification: null, rag: null, diagnosis: null,
      solution: null, risk: null, execution: null,
      report_path: null, duration_ms: null,
    }
  }
  return _post<Incident>('/incidents', { log, scenario })
}

export async function getIncident(id: string): Promise<Incident> {
  if (USE_MOCK) throw new Error('Mock mode: no real incident')
  return _get<Incident>(`/incidents/${id}`)
}

export async function listIncidents(): Promise<Incident[]> {
  if (USE_MOCK) return []
  return _get<Incident[]>('/incidents')
}

export async function approveIncident(id: string): Promise<void> {
  if (USE_MOCK) return
  await _post(`/incidents/${id}/approve`)
}

export async function rejectIncident(id: string): Promise<void> {
  if (USE_MOCK) return
  await _post(`/incidents/${id}/reject`)
}

export function getReportUrl(id: string): string {
  return `${BASE}/incidents/${id}/report`
}

export async function getStats(): Promise<StatsData> {
  if (USE_MOCK) return _mockStats()
  return _get<StatsData>('/stats')
}

export async function listPlaybooks(params?: {
  category?: string
  search?: string
  limit?: number
}): Promise<PlaybookItem[]> {
  if (USE_MOCK) {
    const { playbooks } = await import('./mock-data')
    return playbooks.map((p, i) => ({
      id:               `mock-${i}`,
      title:            p.title,
      category:         p.category,
      severity_typical: null,
      resolution_steps: p.previewLines,
      code_fix:         null,
      source:           null,
    }))
  }
  const qs = new URLSearchParams()
  if (params?.category) qs.set('category', params.category)
  if (params?.search)   qs.set('search',   params.search)
  if (params?.limit)    qs.set('limit',    String(params.limit))
  const query = qs.toString()
  return _get<PlaybookItem[]>(`/playbooks${query ? `?${query}` : ''}`)
}

export { USE_MOCK }
