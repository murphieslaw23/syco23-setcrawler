export type SetSource = 'youtube' | 'soundcloud' | 'freeteknomusic'
export type ReviewStatus = 'inbox' | 'reviewing' | 'accepted' | 'rejected' | 'published'
export type ImportJobStatus = 'queued' | 'processing' | 'retry' | 'completed' | 'failed' | 'blocked' | 'dead_letter'
export type ImportJobType = 'url_import' | 'search_profile' | 'crawl'

export interface Candidate {
  id: string
  field_name: string
  candidate_value: string
  confidence: number
  source: string
  accepted: boolean | null
}

export interface SetImage {
  id: string
  remote_url: string | null
  kind: 'flyer' | 'artist' | 'crew' | 'label' | 'thumbnail'
  attribution: string | null
  is_primary: boolean
}

export interface SetRecord {
  id: string
  source: SetSource
  source_id: string
  canonical_url: string
  title: string
  description?: string | null
  duration_seconds: number
  published_at: string
  set_score: number
  review_status: ReviewStatus
  artist_names: string[]
  event_name: string | null
  venue?: string | null
  city: string | null
  year?: number | null
  primary_image_url: string | null
  raw_payload?: Record<string, unknown>
  candidates?: Candidate[]
  images?: SetImage[]
  artwork_index?: number
  score_reasons?: string[]
  import_job_id?: string | null
  duplicate_of_id?: string | null
}

export interface SetPage {
  items: SetRecord[]
  total: number
  limit: number
  offset: number
}

export interface Stats {
  total_sets: number
  by_source: Record<SetSource, number>
  by_status: Record<ReviewStatus, number>
  score_bands: { high: number; review: number; low: number }
  queue: { queued: number; processing: number; failed: number; completed?: number; retry?: number; blocked?: number }
}

export interface ImportJob {
  id: string
  url: string | null
  source: SetSource
  job_type: ImportJobType
  profile_id: string | null
  status: ImportJobStatus
  attempt_count: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  next_retry_at: string | null
  result_set_id: string | null
  error_code: string | null
  error_message: string | null
  details: Record<string, unknown>
}

export interface ImportJobPage {
  items: ImportJob[]
  total: number
  limit: number
  offset: number
}

export interface ProviderStatus {
  configured: boolean
  enabled: boolean
  mode: string
  display_name?: string
  capabilities?: string[]
  workloads?: Record<string, string>
  configuration_complete?: boolean
  effective_enabled?: boolean
  database_enabled?: boolean
  reason?: string | null
}

export type ProviderHealthStatus = Record<SetSource, ProviderStatus>

export interface SearchProfile {
  id: string
  name: string
  query: string
  source: string
  operation: string
  parameters: Record<string, unknown>
  schedule_cron: string
  last_run_at: string | null
  last_scheduled_at: string | null
  next_scheduled_at: string | null
  next_page_token: string | null
  last_result_count: number | null
  last_error_code: string | null
  latest_job_id: string | null
  enabled: boolean
}
