# LogPulse AI — Frontend Design Specification

## 1. Design Tokens

### Colors

#### Foundational Surfaces
| Token | Value | Usage |
|---|---|---|
| `background` | `#000000` | True-black root background |
| `surface` | `#121212` | Card and panel backgrounds |
| `surface-elevated` | `#1a1a1a` | Modals, dropdowns |
| `border` | `#222222` | All structural 1px borders |
| `border-active` | `#8B5CF6` | Focused/active container border |

#### Functional Accents
| Token | Hex | Semantic Role |
|---|---|---|
| `accent-ai` | `#8B5CF6` | AI insights, ML ops, primary buttons |
| `accent-success` | `#10B981` | Healthy state, successful deploys |
| `accent-warning` | `#F59E0B` | Non-critical warnings, throttling |
| `accent-critical` | `#EF4444` | Critical failures, downtime, CRITICAL badges |
| `accent-info` | `#3B82F6` | Informational, neutral annotations |

#### Text
| Token | Value | Usage |
|---|---|---|
| `text-primary` | `#FFFFFF` | Primary content, active nav |
| `text-secondary` | `rgba(255,255,255,0.5)` | Inactive nav, metadata |
| `text-muted` | `rgba(255,255,255,0.3)` | Disabled, placeholder |

#### Severity Badge Fills (15% opacity)
| Severity | Background | Border | Text |
|---|---|---|---|
| CRITICAL | `rgba(239,68,68,0.15)` | `#EF4444` | `#EF4444` |
| WARN | `rgba(245,158,11,0.15)` | `#F59E0B` | `#F59E0B` |
| INFO | `rgba(59,130,246,0.15)` | `#3B82F6` | `#3B82F6` |
| SUCCESS | `rgba(16,185,129,0.15)` | `#10B981` | `#10B981` |

---

### Typography

#### Font Families
- **Inter** — UI chrome, navigation, body prose, headings
- **JetBrains Mono** — All log output, code, metric values, timestamps, data tables

#### Type Scale
| Token | Family | Size | Weight | Line-H | Tracking |
|---|---|---|---|---|---|
| `display-lg` | Inter | 32px | 700 | 40px | -0.02em |
| `display-lg-mobile` | Inter | 24px | 700 | 32px | -0.01em |
| `headline-md` | Inter | 20px | 600 | 28px | — |
| `body-md` | Inter | 14px | 400 | 20px | — |
| `code-sm` | JetBrains Mono | 12px | 400 | 18px | — |
| `label-caps` | JetBrains Mono | 10px | 600 | 12px | 0.05em |
| `data-tabular` | JetBrains Mono | 13px | 500 | 16px | — |

**Rules:**
- `label-caps` for all table headers and section metadata labels
- No italics in technical data — use weight or color shifts
- All numeric values, timestamps, IDs → JetBrains Mono

---

### Spacing
| Token | Value |
|---|---|
| `unit` | 4px |
| `gutter` | 16px |
| `margin-desktop` | 24px |
| `sidebar-width` | 240px |
| `top-nav-height` | 48px |

All padding/margin values must be multiples of 4px.

---

### Borders & Shape
- **Border radius:** 0px everywhere — strictly sharp corners
- **Border width:** 1px solid
- **Default border color:** `#222222`
- **Active/focus border:** `#8B5CF6` (Indigo)
- No shadows, no blurs, no gradients

---

### Status Indicator Animation
```css
/* Pulsing dot for live/critical services */
@keyframes pulse-ring {
  0%   { transform: scale(1);   opacity: 0.15; }
  100% { transform: scale(2.5); opacity: 0; }
}

.status-dot-critical {
  width: 8px; height: 8px;
  background: #EF4444;
  border-radius: 50%;
  position: relative;
}
.status-dot-critical::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: #EF4444;
  animation: pulse-ring 2s ease-out infinite;
}

.status-dot-healthy { background: #10B981; }
.status-dot-warning { background: #F59E0B; }
```

---

## 2. Layout Structure

```
┌─────────────────────────────────────────────────────┐
│  TOP NAV (48px fixed)                                │
├───────────────┬─────────────────────────────────────┤
│               │                                     │
│  SIDEBAR      │  MAIN CONTENT                       │
│  (240px fixed)│  (fluid, 12-col grid, 16px gutters) │
│               │                                     │
│               │                                     │
└───────────────┴─────────────────────────────────────┘
```

### Top Nav
- Height: 48px
- Background: `#000000`
- Bottom border: `1px solid #222222`
- Left: Logo + app name (Inter 14px 600)
- Center: Page title tabs
- Right: Settings icon + Notifications icon

### Sidebar Navigation (corrected)
Links (in order):
1. Dashboard → `/`
2. Incidents → `/incidents`
3. Playbooks → `/playbooks`
4. Reports → `/reports`

Active link: `text-primary` at full opacity, left `2px solid #8B5CF6` indicator.
Inactive links: `rgba(255,255,255,0.5)`.

---

## 3. Screens

### 3.1 Login — `/login`
**Purpose:** Authentication gate, UI-only, redirects immediately to `/` on submit.

**Layout:** Centered card (400px wide) on black background.

**Components:**
- `LoginCard` — contains the full form
  - App logo + "LogPulse AI" title (Inter 20px 600)
  - Subtitle: "System Authentication Portal v2.4.1" (JetBrains Mono 12px, muted)
  - Status dot (healthy green, pulsing)
  - `TextInput` label="OPERATOR ID" placeholder="admin@cluster-01"
  - `TextInput` label="ACCESS KEY" type="password" rightSlot="Reset Key link"
  - `Checkbox` label="Maintain session"
  - `Button` variant="primary" fullWidth → "AUTHENTICATE SESSION →"
  - Footer: "Unauthorized access is strictly prohibited. All connection attempts are logged." (JetBrains Mono 11px, muted, centered)

**Behavior:** Form submit → `router.push('/')` (no API call).

---

### 3.2 Dashboard — `/`
**Purpose:** Single-pane overview of cluster health, live metrics, and ingestion entry points.

**Layout:** Top stat bar + two-column body (chart left, anomaly panel right) + ingestion row.

**Sections:**

#### Stat Bar (4 cards, equal width)
| Card | Icon | Value | Label |
|---|---|---|---|
| Total Incidents | `AlertTriangle` | `247` | "TOTAL INCIDENTS" |
| Avg Resolution Time | `Zap` | `38.2s` | "AVG RESOLUTION TIME" |
| AI Synthesis Accuracy | `Activity` | `94.3%` | "AI SYNTHESIS ACCURACY" |
| Cluster Uptime | `TrendingUp` | `99.97%` | "CLUSTER UPTIME" + HEALTHY badge |

Active card (Total Incidents) → `border: 1px solid #8B5CF6`.

#### Incident Trends Chart
- `recharts` AreaChart, last 24h
- Series: Memory (crimson), DB (amber), Security (indigo), Node (blue)
- Legend inline top-right
- Background: `#121212`, no axes gridlines except horizontal

#### Anomaly Distribution Panel
- Donut chart center label = total count
- Legend rows: OOM Kills 120 | Query Timeouts 85 | Auth Fails 42

#### Top Performing Playbooks
- Two rows: `pb_restart_db_nodes.yaml` 98% | `pb_scale_up_workers.yaml` 92%
- `[Simulate Live Cluster Data]` ghost button

#### Ingestion Vector Selector (3 cards)
| Card | Badge | Description |
|---|---|---|
| Raw Log Analysis | `FAST` (green) | Textarea + "Run Diagnostic Pipeline →" primary button |
| Live Stream Monitor | `LIVE` (green pulsing dot) | "Awaiting Connection…" status |
| Batch File Upload | `BATCH` (indigo) | Drop zone for `.gz`, `.tar`, `.log` |

---

### 3.3 Pipeline View — `/incidents/[id]`
**Purpose:** Real-time AI analysis execution log for a single incident.

**Layout:** Left stream panel (65%) + right execution panel (35%).

**Left Panel — Analysis Stream**
- Header: "ANALYSIS ENGINE ACTIVE" + playback controls (rewind / pause / forward) + speed badge "1.0×"
- Scrolling log entries, each prefixed with a colored badge:
  - `[INGEST]` indigo
  - `[CLASSIFY]` amber
  - `[RAG]` blue
  - `[DIAGNOSE]` purple
  - `[RISK]` crimson (highlighted row background `rgba(239,68,68,0.08)`)
  - `[SYNTH]` green

**Right Panel — Execution Pipeline**
- `PipelineStepList`: ordered steps with status icons
  - Done: `CheckCircle` emerald
  - Active: `Zap` indigo pulsing
  - Pending: hollow circle muted
- Steps: Ingestion → Log Parsing & Structuring → Semantic Search (RAG) → Root Cause Synthesis → Remediation Drafting → Security Assessment → Done

- `Tabs`: Diagnostic | Remediation | Security

**Diagnostic Tab:**
- AI Confidence Score progress bar (94%)
- `ProposedPatch` component: file label + diff view (green `+`, red `-`)
- `SecurityAssessment` infobox: shield icon + risk level text

**Footer Bar:**
- PDF icon + filename + "Download PDF Report" link
- "Reject & Dismiss" ghost button
- "Approve & Execute Patch" primary button (emerald fill)

---

### 3.4 Live Stream Monitor — `/stream`
**Purpose:** Real-time log ingestion visualization with AI anomaly detection.

**Layout:** Three-column at ≥1280px — left log feed | center detection theater | right telemetry panel.

**Left — Live Log Feed**
- Header: green pulsing dot + "LIVE STREAM: cluster-01" + filter icon
- Scrolling rows: `timestamp [LEVEL] message`
  - CRIT rows: left `2px solid #EF4444` border + `rgba(239,68,68,0.08)` bg
  - ERR rows: amber tint
  - WARN rows: amber/50 tint
- Font: JetBrains Mono 12px

**Center — Detection Theater**
- Header: "DETECTION THEATER" | "CRITICAL STATE" dot
- Canvas/SVG heatmap grid — anomaly highlighted as red rectangle
- Footer: AI Orchestration Pipeline Active + T-minus counter
- `AnomalyCard`:
  - Title: "Memory Exhaustion Cascade"
  - Description prose
  - Stats row: Confidence `98.4%` | Impact Area `Revenue Crit.`
  - "INSPECT ROOT-CAUSE WORKFLOW →" primary button → navigates to `/incidents/[id]`

**Right — AI Engine Telemetry**
- `InferenceGauge`: circular progress, value `95%`, label "Model Drift Nominal"
- `ErrorConcentration`: bar list per service (payment-gw 42%, auth-service 28%, db-replica-02 15%)
- `RecentSignatures`: list of signature IDs + label + time ago
  - SIG-884A | CPU Spurious Wait | -2m
  - SIG-221B | DNS Resolution Latency | -15m
  - SIG-099F | Disk I/O Saturation | -42m
  - SIG-110A | Minor Packet Loss | -1h

---

### 3.5 Batch Archive — `/archive`
**Purpose:** Forensic post-mortem for bulk log bundles.

**Layout:** Full-width stack — drop zone → progress bar → results table → footer summary.

**Sections:**

**Drop Zone**
- `FolderOpen` icon (Lucide)
- "Drag and drop diagnostic bundle here"
- "Supported formats: .log, .json, .gz (Max 5GB)"
- Dashed `#222222` border, hover → `#8B5CF6` border

**Progress Bar**
- Label: "Processing Log 89 of 247…" | percentage right
- Indigo fill, black track, 4px height

**Results Table**
Columns: TIMESTAMP | SEVERITY TAG | TARGET MICROSERVICE | AI MITIGATION STATUS
- `SeverityBadge` component in the severity column
- Pulsing red dot prefix for CRITICAL rows in status column
- Font: JetBrains Mono throughout

**Footer Summary**
- "47,188 logs evaluated." (Inter 24px 700)
- Warning icon + "3 Critical Faults Isolated." (crimson)
- "Download Full Forensic Post-Mortem Bundle" primary button

---

### 3.6 Playbooks Library — `/playbooks`
**Purpose:** Browse, search, and execute runbook automations. Clicking a card slides open a detail drawer on the right.

**Layout:** Two-zone split — left list zone (fluid) + right detail drawer (400px, slides in over content, does not push layout).

---

#### Left Zone — Library Grid

**Search Bar**
- Full-width, `Search` Lucide icon left, placeholder "Search playbooks, commands, syntax..."
- bg `#000000`, border `1px solid #222222`, focus border `#8B5CF6`
- JetBrains Mono input text, 0px border-radius

**Filter Chips** (inline row, right of search bar)
- Chips: ALL | Memory | Security | Database | Network
- Active chip: solid `#8B5CF6` bg, white text
- Inactive chip: `#000000` bg, `1px solid #222222` border, muted text
- 0px border-radius

**Playbook Cards Grid** — 3 columns desktop, 2 tablet, 1 mobile; `gap-4`

Each `PlaybookCard`:
- bg `#121212`, border `1px solid #222222`, hover border `#8B5CF6`
- **Header row:** title (Inter 14px 600) + category badge (right-aligned)
- **Execution count row:** `Clock` Lucide icon + "{N}k executions" (JetBrains Mono 12px muted)
- **Script preview block:** bg `#0a0a0a`, border `1px solid #1a1a1a`, `code-block` class
  - 3 numbered lines of command text, JetBrains Mono 11px
  - Line numbers: `rgba(255,255,255,0.25)`, command text: `rgba(255,255,255,0.85)`
  - Commands truncated with `…` if too long
- Entire card is clickable → opens detail drawer for that playbook

**Category Badge Colors** (on cards):
| Tag | Color |
|---|---|
| MEMORY | `#8B5CF6` indigo |
| DATABASE | `#F59E0B` amber |
| SECURITY | `#EF4444` crimson |
| NETWORK | `#3B82F6` blue |
All use 15% opacity fill + 1px matching border, JetBrains Mono 10px 600, uppercase.

**Mock playbook data:**
| Title | Tag | Executions | Preview commands |
|---|---|---|---|
| Pod OOMKilled Mitigation Pattern | MEMORY | 12.4k | `kubectl describe pod -l app=redis` / `grep -i "oom" /var/log/syslog` / `scale --replicas=3 deployment/redis` |
| DB Connection Pool Exhaustion | DATABASE | 8.1k | `psql -U admin -c "SELECT count(*)..."` / `systemctl restart pgbouncer` / `tail -f /var/log/postgresql/main.log` |
| Unauthorized Access Spike | SECURITY | 3.2k | `awk '$9 == 403' access.log` / `iptables -A INPUT -s <ip> -j DROP` / `notify sec-ops-channel` |
| High Latency on Ingress Node | NETWORK | 15.6k | `netstat -an \| grep 80 \| wc -l` / `iperf3 -c target-server -p 5201` / `restart ingress-nginx-controller` |

---

#### Right Zone — Detail Drawer

Slides in from the right (Framer Motion `x: 400 → 0`). Does not push the grid — overlays at `z-50`.

**Header**
- Title: playbook name (Inter 20px 600)
- `X` Lucide close button top-right, `rgba(255,255,255,0.5)` → white on hover

**DESCRIPTION section**
- Label: "DESCRIPTION" (`label-caps` style — JetBrains Mono 10px 600, uppercase, `0.05em` tracking, muted)
- Body: prose description text (Inter 14px, `rgba(255,255,255,0.7)`)
- Example: "Automated runbook for identifying and mitigating OutOfMemory errors in Kubernetes pods. Analyzes system logs, describes pod events, and provides scaling options to redistribute memory pressure."

**EXECUTION HISTORY (LAST 24H)**
- Label: `label-caps`
- `recharts` BarChart — x-axis: 00:00 → 12:00 → 24:00
- Bar color: `#8B5CF6` with active bar `#EF4444` (brown-red for the spike)
- bg `#0a0a0a`, no gridlines, minimal axes, height ~120px

**FULL SCRIPT SOURCE**
- Label: "FULL SCRIPT SOURCE" (`label-caps`)
- Code block: bg `#0a0a0a`, border `1px solid #222222`, padding `12px`, overflow-y scroll, max-height `240px`
- Line numbers left column: JetBrains Mono 11px `rgba(255,255,255,0.25)`, right-aligned, min-width `24px`
- Code text: JetBrains Mono 12px
- Syntax highlighting:
  - Shebang / keywords (`if`, `then`, `else`, `fi`): `rgba(255,255,255,0.5)` muted
  - Comments (`#`): `#10B981` emerald
  - Variables (`$POD_NAME`, `$NAMESPACE`): `#8B5CF6` indigo
  - Strings / echo values: `#F59E0B` amber
  - Commands: `rgba(255,255,255,0.9)`

Example script (15 lines):
```bash
01  #!/bin/bash
02  # Author: OpsTeam-Alpha
03
04  POD_NAME=$1
05  NAMESPACE=${2:-default}
06
07  echo "Analyzing OOM events for pod $POD_NAME in $NAMESPACE"
08  kubectl describe pod $POD_NAME -n $NAMESPACE | grep -i -A 5 "OOMKilled"
09
10  if [ $? -eq 0 ]; then
11    echo "OOM Event Detected. Recommend scaling..."
12    # Auto-mitigation logic goes here
13  else
14    echo "No recent OOM events found."
15  fi
```

**Footer** (sticky bottom of drawer, `border-top: 1px solid #222222`)
- "Edit Playbook" ghost button (left)
- "Run Now" primary button with `Play` Lucide icon (right), bg `#10B981` emerald (not indigo — execution action)

---

#### `PlaybookCard` component props
```tsx
interface PlaybookCardProps {
  id: string
  title: string
  category: 'MEMORY' | 'DATABASE' | 'SECURITY' | 'NETWORK'
  executionCount: string     // pre-formatted, e.g. "12.4k"
  previewLines: string[]     // exactly 3 command strings
  onClick: () => void
  active?: boolean           // border turns indigo when drawer is open for this card
}
```

#### `PlaybookDrawer` component props
```tsx
interface PlaybookDrawerProps {
  playbook: Playbook | null  // null = drawer closed
  onClose: () => void
}
```

---

## 4. Reusable Components

### `<Button>`
```tsx
interface ButtonProps {
  variant: 'primary' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'lg'
  fullWidth?: boolean
  icon?: React.ReactNode        // Lucide icon, left-side
  trailingIcon?: React.ReactNode
  disabled?: boolean
  onClick?: () => void
  children: React.ReactNode
}
```
- `primary`: bg `#8B5CF6`, text white, border none
- `ghost`: bg transparent, border `1px solid #222222`, text `rgba(255,255,255,0.7)`
- `danger`: bg `rgba(239,68,68,0.15)`, border `#EF4444`, text `#EF4444`
- All: 0px border-radius, Inter 14px 500, uppercase tracking

---

### `<SeverityBadge>`
```tsx
interface SeverityBadgeProps {
  level: 'CRITICAL' | 'WARN' | 'INFO' | 'SUCCESS'
}
```
- JetBrains Mono 10px 600, uppercase
- Background 15% opacity of status color
- 1px solid border at 100% status color
- 0px border-radius

---

### `<StatusDot>`
```tsx
interface StatusDotProps {
  status: 'healthy' | 'warning' | 'critical' | 'offline'
  pulse?: boolean   // enables CSS animation for live indicators
  size?: number     // default 8px
}
```

---

### `<TextInput>`
```tsx
interface TextInputProps {
  label: string
  placeholder?: string
  type?: 'text' | 'password' | 'search'
  value: string
  onChange: (v: string) => void
  rightSlot?: React.ReactNode
  error?: string
  monospace?: boolean   // switches to JetBrains Mono
}
```
- bg `#000000`, border `1px solid #222222`
- Focus: border `#8B5CF6`
- 0px border-radius

---

### `<PipelineStepList>`
```tsx
interface PipelineStep {
  label: string
  status: 'done' | 'active' | 'pending'
}
interface PipelineStepListProps {
  steps: PipelineStep[]
}
```

---

### `<LogRow>`
```tsx
interface LogRowProps {
  timestamp: string
  level: 'CRIT' | 'ERR' | 'WARN' | 'INFO'
  message: string
}
```
- Font: JetBrains Mono 12px
- CRIT: left 2px crimson border + crimson tint bg
- ERR: amber tint bg
- WARN: amber/30 tint bg

---

### `<AgentBadge>`
```tsx
interface AgentBadgeProps {
  agent: 'INGEST' | 'CLASSIFY' | 'RAG' | 'DIAGNOSE' | 'RISK' | 'SYNTH'
}
```
Color map:
- INGEST → indigo `#8B5CF6`
- CLASSIFY → amber `#F59E0B`
- RAG → blue `#3B82F6`
- DIAGNOSE → purple `#A855F7`
- RISK → crimson `#EF4444`
- SYNTH → emerald `#10B981`

---

### `<ProposedPatch>`
```tsx
interface ProposedPatchProps {
  filename: string
  diff: Array<{ type: 'add' | 'remove' | 'context'; content: string }>
}
```
- bg `#0a0a0a`, JetBrains Mono 12px
- `add` lines: emerald text
- `remove` lines: crimson text
- `context` lines: muted gray

---

### `<MetricCard>`
```tsx
interface MetricCardProps {
  label: string
  value: string
  icon: React.ReactNode
  active?: boolean       // indigo border when true
  statusBadge?: string   // e.g. "HEALTHY"
}
```

---

### `<DropZone>`
```tsx
interface DropZoneProps {
  accept: string[]
  maxSize: number        // bytes
  onDrop: (files: File[]) => void
  uploading?: boolean
}
```

---

## 5. TypeScript Data Models

```typescript
// Incident
export interface Incident {
  id: string
  timestamp: string          // ISO 8601
  severity: 'CRITICAL' | 'WARN' | 'INFO'
  service: string
  title: string
  rootCause?: string
  confidence?: number        // 0–1
  status: 'open' | 'resolved' | 'dismissed'
  mitigationStatus?: string
}

// Pipeline Step
export interface PipelineStepData {
  id: string
  label: string
  status: 'done' | 'active' | 'pending' | 'error'
  startedAt?: string
  completedAt?: string
}

// Analysis Stream Entry
export interface StreamEntry {
  id: string
  agent: 'INGEST' | 'CLASSIFY' | 'RAG' | 'DIAGNOSE' | 'RISK' | 'SYNTH'
  content: string
  timestamp: string
  highlighted?: boolean
}

// Log Row
export interface LogEntry {
  id: string
  timestamp: string          // HH:MM:SS format for display
  level: 'CRIT' | 'ERR' | 'WARN' | 'INFO'
  message: string
  pod?: string
}

// Proposed Patch
export interface DiffLine {
  type: 'add' | 'remove' | 'context'
  content: string
}

export interface ProposedPatchData {
  filename: string
  diff: DiffLine[]
}

// Playbook
export interface Playbook {
  id: string
  title: string
  category: 'MEMORY' | 'DATABASE' | 'SECURITY' | 'NETWORK'
  executionCount: string     // pre-formatted display string, e.g. "12.4k"
  previewLines: string[]     // 3 command strings shown on card
  description: string
  scriptSource: string       // full bash/shell script text
  executionHistory: Array<{ hour: string; count: number }>  // 24 bars for chart
}

// Anomaly Signature
export interface AnomalySignature {
  id: string                 // e.g. "SIG-884A"
  label: string
  detectedAt: string         // ISO 8601
  confidence: number
  impactArea: string
}

// Cluster Metrics
export interface ClusterMetrics {
  totalIncidents: number
  avgResolutionSeconds: number
  aiAccuracy: number         // 0–100
  uptimePercent: number
  anomalyBreakdown: {
    oomKills: number
    queryTimeouts: number
    authFails: number
  }
}

// Batch Archive Row
export interface ArchiveRow {
  timestamp: string
  severity: 'CRITICAL' | 'WARN' | 'INFO'
  service: string
  mitigationStatus: string
  isPulsing?: boolean
}
```

---

## 6. Routing & File Structure

```
src/
  app/
    layout.tsx              # Root layout (sidebar + topnav shell)
    page.tsx                # Dashboard /
    login/
      page.tsx              # Login /login
    incidents/
      [id]/
        page.tsx            # Pipeline View /incidents/[id]
    stream/
      page.tsx              # Live Stream Monitor /stream
    archive/
      page.tsx              # Batch Archive /archive
    playbooks/
      page.tsx              # Playbooks Library /playbooks
    reports/
      page.tsx              # Reports (placeholder)
  components/
    layout/
      TopNav.tsx
      Sidebar.tsx
      Shell.tsx
    ui/
      Button.tsx
      SeverityBadge.tsx
      StatusDot.tsx
      TextInput.tsx
      AgentBadge.tsx
      ProposedPatch.tsx
      MetricCard.tsx
      DropZone.tsx
      LogRow.tsx
      PipelineStepList.tsx
  lib/
    mock-data.ts            # All static demo data
    types.ts                # All TypeScript interfaces (above)
    utils.ts
```

---

## 7. Key Implementation Notes

1. **No emoji anywhere** — use only Lucide React icons.
2. **No border-radius** — all `rounded-none` in Tailwind, override shadcn defaults.
3. **Login is UI-only** — `onSubmit` calls `router.push('/')`.
4. **CRITICAL always red** — `#EF4444` with 15% opacity fill, never orange or any other color.
5. **JetBrains Mono** for: timestamps, log messages, IDs, metric values, code blocks, badge text.
6. **Inter** for: nav labels, headings, body copy, button labels.
7. **Status dots** — CSS `::after` pseudo-element for pulse ring, no JS animation library.
8. **shadcn components** must have their border-radius overridden to 0 via `globals.css` variable `--radius: 0`.
9. **Framer Motion** for: analysis stream entry reveal (staggered fade-in), pipeline step transitions.
10. **Recharts** surfaces must use `#121212` background, no default white fill.
