export type Severity = 'CRITICAL' | 'WARN' | 'INFO' | 'SUCCESS'

export type AgentType = 'INGEST' | 'CLASSIFY' | 'RAG' | 'DIAGNOSE' | 'RISK' | 'SYNTH'

export type PipelineStepStatus = 'done' | 'active' | 'pending' | 'error'

export type PlaybookCategory = 'MEMORY' | 'DATABASE' | 'SECURITY' | 'NETWORK'

export type LogLevel = 'CRIT' | 'ERR' | 'WARN' | 'INFO'

export type IncidentStatus = 'open' | 'resolved' | 'dismissed'

// ─── Cluster / Dashboard ─────────────────────────────────────────────────────

export interface ClusterMetrics {
  totalIncidents: number
  avgResolutionSeconds: number
  aiAccuracy: number
  uptimePercent: number
  anomalyBreakdown: {
    oomKills: number
    queryTimeouts: number
    authFails: number
  }
}

export interface TrendPoint {
  time: string
  memory: number
  db: number
  security: number
  node: number
}

export interface TopPlaybook {
  name: string
  successRate: number
}

// ─── Incidents / Pipeline ─────────────────────────────────────────────────────

export interface Incident {
  id: string
  timestamp: string
  severity: Severity
  service: string
  title: string
  rootCause?: string
  confidence?: number
  status: IncidentStatus
  mitigationStatus?: string
}

export interface PipelineStep {
  id: string
  label: string
  status: PipelineStepStatus
}

export interface StreamEntry {
  id: string
  agent: AgentType
  content: string
  timestamp: string
  highlighted?: boolean
}

export interface DiffLine {
  type: 'add' | 'remove' | 'context'
  content: string
}

export interface ProposedPatchData {
  filename: string
  diff: DiffLine[]
}

// ─── Live Stream ──────────────────────────────────────────────────────────────

export interface LogEntry {
  id: string
  timestamp: string
  level: LogLevel
  message: string
  pod?: string
}

export interface AnomalySignature {
  id: string
  label: string
  minutesAgo: number
}

export interface ErrorConcentration {
  service: string
  percent: number
}

// ─── Archive ──────────────────────────────────────────────────────────────────

export interface ArchiveRow {
  id: string
  timestamp: string
  severity: Severity
  service: string
  mitigationStatus: string
  isPulsing?: boolean
}

// ─── Playbooks ────────────────────────────────────────────────────────────────

export interface Playbook {
  id: string
  title: string
  category: PlaybookCategory
  executionCount: string
  previewLines: string[]
  description: string
  scriptSource: string
  executionHistory: Array<{ hour: string; count: number }>
}
