import type {
  ClusterMetrics,
  TrendPoint,
  TopPlaybook,
  Incident,
  PipelineStep,
  StreamEntry,
  ProposedPatchData,
  LogEntry,
  AnomalySignature,
  ErrorConcentration,
  ArchiveRow,
  Playbook,
} from './types'

// ─── Dashboard ────────────────────────────────────────────────────────────────

export const clusterMetrics: ClusterMetrics = {
  totalIncidents: 247,
  avgResolutionSeconds: 38.2,
  aiAccuracy: 94.3,
  uptimePercent: 99.97,
  anomalyBreakdown: {
    oomKills: 120,
    queryTimeouts: 85,
    authFails: 42,
  },
}

export const incidentTrends: TrendPoint[] = [
  { time: '00:00', memory: 4,  db: 2,  security: 1,  node: 3  },
  { time: '02:00', memory: 6,  db: 3,  security: 2,  node: 4  },
  { time: '04:00', memory: 3,  db: 5,  security: 1,  node: 2  },
  { time: '06:00', memory: 8,  db: 4,  security: 3,  node: 5  },
  { time: '08:00', memory: 12, db: 7,  security: 4,  node: 6  },
  { time: '10:00', memory: 15, db: 9,  security: 5,  node: 8  },
  { time: '12:00', memory: 20, db: 12, security: 8,  node: 10 },
  { time: '14:00', memory: 18, db: 10, security: 6,  node: 9  },
  { time: '16:00', memory: 22, db: 15, security: 7,  node: 11 },
  { time: '18:00', memory: 28, db: 18, security: 10, node: 13 },
  { time: '20:00', memory: 24, db: 14, security: 9,  node: 12 },
  { time: '22:00', memory: 30, db: 20, security: 12, node: 15 },
  { time: '24:00', memory: 26, db: 16, security: 8,  node: 11 },
]

export const topPlaybooks: TopPlaybook[] = [
  { name: 'pb_restart_db_nodes.yaml',  successRate: 98 },
  { name: 'pb_scale_up_workers.yaml',  successRate: 92 },
]

// ─── Incidents ────────────────────────────────────────────────────────────────

export const incidents: Incident[] = [
  {
    id: 'inc-001',
    timestamp: '2023-10-27T14:32:01.442Z',
    severity: 'CRITICAL',
    service: 'pod-auth-service-7f8b9',
    title: 'NullReferenceException in TokenValidation',
    rootCause: 'Authentication service failing to acquire read lock on Redis cluster due to network partition at 14:31:55 UTC',
    confidence: 0.94,
    status: 'open',
    mitigationStatus: 'Patch proposed',
  },
  {
    id: 'inc-002',
    timestamp: '2023-10-27T14:02:12Z',
    severity: 'CRITICAL',
    service: 'pod-payment-gw-7b9',
    title: 'OOMKilled — Memory Exhaustion Cascade',
    rootCause: 'Memory spike preceding OOMKilled event on payment gateway',
    confidence: 0.984,
    status: 'open',
    mitigationStatus: 'Isolating',
  },
  {
    id: 'inc-003',
    timestamp: '2023-10-27T13:45:00Z',
    severity: 'WARN',
    service: 'user-profile-db',
    title: 'Query Timeout Spike',
    confidence: 0.87,
    status: 'resolved',
    mitigationStatus: 'Query optimized',
  },
]

// ─── Pipeline (incident inc-001) ──────────────────────────────────────────────

export const pipelineSteps: PipelineStep[] = [
  { id: 'ingestion',     label: 'Ingestion',                 status: 'done'    },
  { id: 'parsing',       label: 'Log Parsing & Structuring', status: 'done'    },
  { id: 'rag',           label: 'Semantic Search (RAG)',      status: 'done'    },
  { id: 'synthesis',     label: 'Root Cause Synthesis',       status: 'active'  },
  { id: 'remediation',   label: 'Remediation Drafting',       status: 'pending' },
  { id: 'security',      label: 'Security Assessment',        status: 'pending' },
  { id: 'done',          label: 'Done',                       status: 'pending' },
]

export const analysisStream: StreamEntry[] = [
  {
    id: 's1',
    agent: 'INGEST',
    content: 'Parsed multi-line stack trace from <span class="text-[#8B5CF6]">pod-auth-service-7f8b9...</span> at 14:32:01.442 UTC. Extracted relevant framing pointers.',
    timestamp: '14:32:01',
  },
  {
    id: 's2',
    agent: 'CLASSIFY',
    content: 'Signature match identified: <span class="text-[#EF4444]">NullReferenceException</span> in <span class="text-[#8B5CF6]">TokenValidation.cs:114</span>. Confidence score: 0.98.',
    timestamp: '14:32:02',
  },
  {
    id: 's3',
    agent: 'RAG',
    content: 'Querying historical playbook DB for similar exception contexts. Found 3 matches in past 30 days related to <span class="text-[#8B5CF6]">RedisCache</span> timeout cascading failures.',
    timestamp: '14:32:03',
  },
  {
    id: 's4',
    agent: 'DIAGNOSE',
    content: 'Root Cause Hypothesis: The authentication service is failing to acquire a read lock on the Redis cluster due to a network partition event occurring at 14:31:55 UTC. The fallback mechanism attempts to read from a null configuration object, causing the crash loop.',
    timestamp: '14:32:04',
  },
  {
    id: 's5',
    agent: 'RISK',
    content: 'SEVERITY: HIGH. Cascading auth failures detected affecting 14% of downstream API traffic. Requires immediate mitigation.',
    timestamp: '14:32:05',
    highlighted: true,
  },
  {
    id: 's6',
    agent: 'SYNTH',
    content: 'Generating remediation payload and shell script variants...',
    timestamp: '14:32:06',
  },
]

export const proposedPatch: ProposedPatchData = {
  filename: 'config-map.yaml',
  diff: [
    { type: 'remove',  content: '- fallback_config: null' },
    { type: 'add',     content: '+ fallback_config:' },
    { type: 'add',     content: '+   mode: "degraded_read"' },
    { type: 'add',     content: '+   timeout_ms: 500' },
  ],
}

// ─── Live Stream ──────────────────────────────────────────────────────────────

export const liveLogEntries: LogEntry[] = [
  { id: 'l1',  timestamp: '14:02:01', level: 'INFO', message: 'kubelet node-1 sync loop...' },
  { id: 'l2',  timestamp: '14:02:02', level: 'INFO', message: 'containerd check complete' },
  { id: 'l3',  timestamp: '14:02:05', level: 'WARN', message: 'memory pressure approaching 80% on pod/payment-gw-7b9' },
  { id: 'l4',  timestamp: '14:02:08', level: 'INFO', message: 'scaling event initiated' },
  { id: 'l5',  timestamp: '14:02:12', level: 'CRIT', message: 'OOMKilled pod/payment-gw-7b9 container=auth', pod: 'payment-gw-7b9' },
  { id: 'l6',  timestamp: '14:02:13', level: 'ERR',  message: 'failed to restart container back-off 10s' },
  { id: 'l7',  timestamp: '14:02:15', level: 'INFO', message: 'health probe failed x3' },
  { id: 'l8',  timestamp: '14:02:20', level: 'INFO', message: 'ingress rerouting traffic...' },
  { id: 'l9',  timestamp: '14:02:22', level: 'INFO', message: 'garbage collection run' },
  { id: 'l10', timestamp: '14:02:25', level: 'WARN', message: 'latency spike > 200ms' },
  { id: 'l11', timestamp: '14:02:30', level: 'INFO', message: 'node-2 joining pool' },
  { id: 'l12', timestamp: '14:02:31', level: 'INFO', message: 'kubelet node-1 sync loop...' },
  { id: 'l13', timestamp: '14:02:32', level: 'INFO', message: 'containerd check complete' },
  { id: 'l14', timestamp: '14:02:35', level: 'WARN', message: 'memory pressure approaching 80% on pod/payment-gw-7b9' },
  { id: 'l15', timestamp: '14:02:42', level: 'CRIT', message: 'OOMKilled pod/payment-gw-7b9 container=auth', pod: 'payment-gw-7b9' },
  { id: 'l16', timestamp: '14:02:43', level: 'ERR',  message: 'failed to restart container back-off 10s' },
  { id: 'l17', timestamp: '14:02:45', level: 'INFO', message: 'health probe failed x3' },
]

export const anomalySignatures: AnomalySignature[] = [
  { id: 'SIG-884A', label: 'CPU Spurious Wait',    minutesAgo: 2  },
  { id: 'SIG-221B', label: 'DNS Resolution Latency', minutesAgo: 15 },
  { id: 'SIG-099F', label: 'Disk I/O Saturation',  minutesAgo: 42 },
  { id: 'SIG-110A', label: 'Minor Packet Loss',    minutesAgo: 60 },
]

export const errorConcentration: ErrorConcentration[] = [
  { service: 'payment-gw',    percent: 42 },
  { service: 'auth-service',  percent: 28 },
  { service: 'db-replica-02', percent: 15 },
]

// ─── Archive ──────────────────────────────────────────────────────────────────

export const archiveRows: ArchiveRow[] = [
  { id: 'a1', timestamp: '2023-10-27T08:14:02Z', severity: 'CRITICAL', service: 'auth-service-v2',   mitigationStatus: 'Isolating token leak', isPulsing: true  },
  { id: 'a2', timestamp: '2023-10-27T08:14:01Z', severity: 'INFO',     service: 'payment-gateway',   mitigationStatus: 'Nominal'              },
  { id: 'a3', timestamp: '2023-10-27T08:13:58Z', severity: 'WARN',     service: 'user-profile-db',   mitigationStatus: 'Query optimized'      },
  { id: 'a4', timestamp: '2023-10-27T08:13:55Z', severity: 'CRITICAL', service: 'auth-service-v2',   mitigationStatus: 'Rate limit breached',  isPulsing: true  },
]

// ─── Playbooks ────────────────────────────────────────────────────────────────

const oomScript = `#!/bin/bash
# Author: OpsTeam-Alpha

POD_NAME=$1
NAMESPACE=\${2:-default}

echo "Analyzing OOM events for pod $POD_NAME in $NAMESPACE"
kubectl describe pod $POD_NAME -n $NAMESPACE | grep -i -A 5 "OOMKilled"

if [ $? -eq 0 ]; then
  echo "OOM Event Detected. Recommend scaling..."
  # Auto-mitigation logic goes here
else
  echo "No recent OOM events found."
fi`

const dbScript = `#!/bin/bash
# Author: OpsTeam-Beta

DB_HOST=\${1:-localhost}
DB_PORT=\${2:-5432}

echo "Checking connection pool on $DB_HOST:$DB_PORT"
psql -U admin -h $DB_HOST -c "SELECT count(*) FROM pg_stat_activity;"

if [ $? -ne 0 ]; then
  echo "Pool exhausted — restarting pgbouncer"
  systemctl restart pgbouncer
fi

tail -f /var/log/postgresql/main.log`

const secScript = `#!/bin/bash
# Author: SecOps-Team

LOG_FILE=\${1:-/var/log/nginx/access.log}

echo "Scanning for 403 spikes in $LOG_FILE"
awk '$9 == 403' $LOG_FILE | awk '{print $1}' | sort | uniq -c | sort -rn | head -10

# Block top offending IPs
for IP in $(awk '$9 == 403 {print $1}' $LOG_FILE | sort | uniq -c | sort -rn | awk 'NR<=3{print $2}'); do
  iptables -A INPUT -s $IP -j DROP
  echo "Blocked: $IP"
done

notify sec-ops-channel "Access spike mitigated"`

const networkScript = `#!/bin/bash
# Author: NetOps-Alpha

TARGET=\${1:-target-server}
PORT=\${2:-5201}

echo "Checking ingress node connections"
netstat -an | grep 80 | wc -l

echo "Running iperf3 throughput test to $TARGET:$PORT"
iperf3 -c $TARGET -p $PORT -t 10

if [ $? -ne 0 ]; then
  echo "High latency detected — restarting nginx controller"
  kubectl rollout restart deployment/ingress-nginx-controller -n ingress-nginx
fi`

function buildHistory(spike: number): Array<{ hour: string; count: number }> {
  return Array.from({ length: 24 }, (_, i) => ({
    hour: `${String(i).padStart(2, '0')}:00`,
    count: i === spike ? 18 : Math.floor(Math.random() * 8) + 2,
  }))
}

export const playbooks: Playbook[] = [
  {
    id: 'pb-001',
    title: 'Pod OOMKilled Mitigation Pattern',
    category: 'MEMORY',
    executionCount: '12.4k',
    previewLines: [
      'kubectl describe pod -l app=redis',
      'grep -i "oom" /var/log/syslog',
      'scale --replicas=3 deployment/redis',
    ],
    description:
      'Automated runbook for identifying and mitigating OutOfMemory errors in Kubernetes pods. Analyzes system logs, describes pod events, and provides scaling options to redistribute memory pressure.',
    scriptSource: oomScript,
    executionHistory: buildHistory(12),
  },
  {
    id: 'pb-002',
    title: 'DB Connection Pool Exhaustion',
    category: 'DATABASE',
    executionCount: '8.1k',
    previewLines: [
      'psql -U admin -c "SELECT count(*)..."',
      'systemctl restart pgbouncer',
      'tail -f /var/log/postgresql/main.log',
    ],
    description:
      'Detects and remediates PostgreSQL connection pool exhaustion. Monitors active connections, restarts pgbouncer when thresholds are exceeded, and tails logs for ongoing diagnostics.',
    scriptSource: dbScript,
    executionHistory: buildHistory(9),
  },
  {
    id: 'pb-003',
    title: 'Unauthorized Access Spike',
    category: 'SECURITY',
    executionCount: '3.2k',
    previewLines: [
      "awk '$9 == 403' access.log",
      'iptables -A INPUT -s <ip> -j DROP',
      'notify sec-ops-channel',
    ],
    description:
      'Identifies and blocks IP addresses generating unauthorized access (403) spikes. Scans nginx access logs, extracts top offenders, applies iptables rules, and notifies the security ops channel.',
    scriptSource: secScript,
    executionHistory: buildHistory(15),
  },
  {
    id: 'pb-004',
    title: 'High Latency on Ingress Node',
    category: 'NETWORK',
    executionCount: '15.6k',
    previewLines: [
      'netstat -an | grep 80 | wc -l',
      'iperf3 -c target-server -p 5201',
      'restart ingress-nginx-controller',
    ],
    description:
      'Diagnoses and resolves high latency on Kubernetes ingress nodes. Measures active connections, runs iperf3 throughput tests, and restarts the nginx-ingress controller if degradation is confirmed.',
    scriptSource: networkScript,
    executionHistory: buildHistory(18),
  },
]
