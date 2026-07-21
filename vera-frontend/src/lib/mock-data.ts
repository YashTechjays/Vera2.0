/** Visual category — drives the row tint, indicator, duration color, and badge. */
export type CallCategory = "critical" | "active" | "processing" | "completed"
export type CallAction = "intervene" | "view" | "add-info"

export type LiveCall = {
  id: string
  patient: string
  type: string
  agent: string
  duration: string
  /** full status label (shown truncated in the cell) */
  status: string
  category: CallCategory
  visible: boolean
  action: CallAction
  // overview-modal detail
  insurance: string
  confidence: number
  formProgress: number
  callTime: string
  /** ISO start time from the API (null until the callee answers) — the modals'
   *  live-timer seed. Absent on mock rows. */
  startedAt?: string | null
  /** Latest observer health score (0-100); null/undefined = not assessed. */
  healthScore?: number | null
}

export const liveCalls: LiveCall[] = [
  {
    id: "c1",
    patient: "Emma Johnson",
    type: "Patient",
    agent: "Riley Voice Bot",
    duration: "06:06",
    status: "CRITICAL - CRITICAL - LOOP DETECTED",
    category: "critical",
    visible: true,
    action: "intervene",
    insurance: "Aetna",
    confidence: 42,
    formProgress: 40,
    callTime: "06:14",
  },
  {
    id: "c2",
    patient: "Liam Williams",
    type: "Patient",
    agent: "Jordan Intake AI",
    duration: "12:06",
    status: "CRITICAL - ERROR - FAILED VERIFICATION",
    category: "critical",
    visible: false,
    action: "intervene",
    insurance: "Cigna",
    confidence: 51,
    formProgress: 25,
    callTime: "12:06",
  },
  {
    id: "c3",
    patient: "Noah Davis",
    type: "Patient",
    agent: "Sam Benefits Bot",
    duration: "02:06",
    status: "ACTIVE - IN PROGRESS",
    category: "active",
    visible: false,
    action: "view",
    insurance: "UnitedHealthcare",
    confidence: 88,
    formProgress: 60,
    callTime: "02:06",
  },
  {
    id: "c4",
    patient: "Ava Miller",
    type: "Patient",
    agent: "Riley Voice Bot",
    duration: "08:06",
    status: "ACTIVE - IN PROGRESS",
    category: "active",
    visible: true,
    action: "view",
    insurance: "Blue Cross",
    confidence: 79,
    formProgress: 55,
    callTime: "08:06",
  },
  {
    id: "c5",
    patient: "Olivia Brown",
    type: "Patient",
    agent: "Casey Verify AI",
    duration: "03:06",
    status: "PROCESSING - MISSING MEMBER ID",
    category: "processing",
    visible: true,
    action: "add-info",
    insurance: "Humana",
    confidence: 64,
    formProgress: 35,
    callTime: "03:06",
  },
]

// ── Data Management — patient verification forms ──────────────────────────────

/** Patient-form lifecycle statuses (ported from smart-caller-fe). */
export type PatientFormStatus =
  | "READY FOR PROCESSING"
  | "IN QUEUE"
  | "IN CALL"
  | "AI PROCESSING"
  | "EXCEPTION REVIEW"
  | "COMPLETED"
  | "CALL FAILED"

export type PatientForm = {
  id: string
  appointmentDate: string
  appointmentType: string
  chartNo: string
  patientName: string
  memberPolicyId: string
  insuranceProvider: string
  status: PatientFormStatus
}

/** Pill styling per status — mirrors the reference's StatusChip colors. */
export const patientStatusStyles: Record<PatientFormStatus, string> = {
  "READY FOR PROCESSING": "bg-muted text-muted-foreground",
  "IN QUEUE": "bg-emerald-100 text-emerald-700",
  "IN CALL": "bg-amber-100 text-amber-800",
  "AI PROCESSING": "bg-cyan-100 text-cyan-700",
  "EXCEPTION REVIEW": "bg-red-100 text-red-700",
  COMPLETED: "bg-emerald-100 text-emerald-700",
  "CALL FAILED": "bg-muted text-muted-foreground",
}

/** Allowed status transitions (ported from the reference's transition matrix). */
export const statusTransitions: Record<PatientFormStatus, PatientFormStatus[]> = {
  "READY FOR PROCESSING": ["IN QUEUE"],
  "IN CALL": [],
  "AI PROCESSING": [],
  "CALL FAILED": ["IN QUEUE"],
  "EXCEPTION REVIEW": ["COMPLETED", "IN QUEUE"],
  "IN QUEUE": [],
  COMPLETED: [],
}

export const patientForms: PatientForm[] = [
  {
    id: "pf1",
    appointmentDate: "06/19/2026",
    appointmentType: "New Patient",
    chartNo: "CH-4500",
    patientName: "Emma Johnson",
    memberPolicyId: "MBR-90000",
    insuranceProvider: "Aetna",
    status: "READY FOR PROCESSING",
  },
  {
    id: "pf2",
    appointmentDate: "06/20/2026",
    appointmentType: "Reverification",
    chartNo: "CH-4501",
    patientName: "Liam Williams",
    memberPolicyId: "MBR-90001",
    insuranceProvider: "UnitedHealthcare",
    status: "IN QUEUE",
  },
  {
    id: "pf3",
    appointmentDate: "06/21/2026",
    appointmentType: "Reverification",
    chartNo: "CH-4502",
    patientName: "Olivia Brown",
    memberPolicyId: "MBR-90002",
    insuranceProvider: "Cigna",
    status: "IN CALL",
  },
  {
    id: "pf4",
    appointmentDate: "06/22/2026",
    appointmentType: "New Patient",
    chartNo: "CH-4503",
    patientName: "Noah Davis",
    memberPolicyId: "MBR-90003",
    insuranceProvider: "Blue Cross",
    status: "AI PROCESSING",
  },
  {
    id: "pf5",
    appointmentDate: "06/23/2026",
    appointmentType: "Reverification",
    chartNo: "CH-4504",
    patientName: "Ava Miller",
    memberPolicyId: "MBR-90004",
    insuranceProvider: "Humana",
    status: "EXCEPTION REVIEW",
  },
  {
    id: "pf6",
    appointmentDate: "06/24/2026",
    appointmentType: "Reverification",
    chartNo: "CH-4505",
    patientName: "Ethan Wilson",
    memberPolicyId: "MBR-90005",
    insuranceProvider: "Kaiser",
    status: "COMPLETED",
  },
  {
    id: "pf7",
    appointmentDate: "06/25/2026",
    appointmentType: "New Patient",
    chartNo: "CH-4506",
    patientName: "Sophia Moore",
    memberPolicyId: "MBR-90006",
    insuranceProvider: "Aetna",
    status: "READY FOR PROCESSING",
  },
  {
    id: "pf8",
    appointmentDate: "06/26/2026",
    appointmentType: "Reverification",
    chartNo: "CH-4507",
    patientName: "Mason Taylor",
    memberPolicyId: "MBR-90007",
    insuranceProvider: "UnitedHealthcare",
    status: "IN QUEUE",
  },
]

export const completedCalls: LiveCall[] = [
  {
    id: "d1",
    patient: "Maria Gonzalez",
    type: "Patient",
    agent: "Riley Voice Bot",
    duration: "04:12",
    status: "COMPLETED - VERIFIED",
    category: "completed",
    visible: false,
    action: "view",
    insurance: "Aetna",
    confidence: 96,
    formProgress: 100,
    callTime: "04:12",
  },
  {
    id: "d2",
    patient: "Daniel Okoro",
    type: "Patient",
    agent: "Sam Benefits Bot",
    duration: "03:47",
    status: "COMPLETED - VERIFIED",
    category: "completed",
    visible: false,
    action: "view",
    insurance: "Cigna",
    confidence: 93,
    formProgress: 100,
    callTime: "03:47",
  },
]
