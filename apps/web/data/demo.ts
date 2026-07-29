import type { Candidate, ImportJob, ProviderHealthStatus, SearchProfile, SetRecord, Stats } from '~/types'

const candidate = (
  setId: number,
  index: number,
  field: string,
  value: string,
  confidence: number
): Candidate => ({
  id: `00000000-0000-4001-8000-${String(setId * 10 + index).padStart(12, '0')}`,
  field_name: field,
  candidate_value: value,
  confidence,
  source: field === 'city' ? 'description_regex' : 'title_regex',
  accepted: null
})

export const demoSets: SetRecord[] = [
  {
    id: '00000000-0000-4000-8000-000000000001',
    source: 'youtube',
    source_id: 'yt-murph-2026',
    canonical_url: 'https://www.youtube.com/watch?v=yt-murph-2026',
    title: 'MURPH @ SOUTH SIDE TEKNIVAL 2026',
    description: 'Recorded at Hangar 23, Berlin on 18.05.2026. Raw underground hardtek liveset.',
    duration_seconds: 5062,
    published_at: '2026-05-18T18:00:00Z',
    set_score: 0.82,
    review_status: 'inbox',
    artist_names: ['MURPH'],
    event_name: 'South Side Teknival',
    venue: 'Hangar 23',
    city: 'Berlin',
    year: 2026,
    primary_image_url: null,
    artwork_index: 0,
    raw_payload: { provider: 'youtube', channel: 'SYCO23 SOURCE NETWORK', tags: ['hardtek', 'liveset'] },
    candidates: [
      candidate(1, 1, 'artist', 'MURPH', 0.94),
      candidate(1, 2, 'event', 'South Side Teknival', 0.88),
      candidate(1, 3, 'date', '2026-05-18', 0.93),
      candidate(1, 4, 'venue', 'Hangar 23', 0.76),
      candidate(1, 5, 'city', 'Berlin', 0.84)
    ]
  },
  {
    id: '00000000-0000-4000-8000-000000000002',
    source: 'soundcloud',
    source_id: 'sc-k-zmk',
    canonical_url: 'https://soundcloud.com/syco23/k-zmk-free-party',
    title: 'K- - B2B ZMK — FREE PARTY SESSION',
    description: 'Recorded at La Zone Libre, Brussels.',
    duration_seconds: 4365,
    published_at: '2026-05-17T18:00:00Z',
    set_score: 0.65,
    review_status: 'inbox',
    artist_names: ['K- -', 'ZMK'],
    event_name: 'Free Party Session',
    venue: 'La Zone Libre',
    city: 'Brussels',
    year: 2026,
    primary_image_url: null,
    artwork_index: 1,
    raw_payload: { provider: 'soundcloud', uploader: 'K- -', tags: ['tribe', 'b2b'] },
    candidates: [
      candidate(2, 1, 'artist', 'K- -', 0.82),
      candidate(2, 2, 'artist', 'ZMK', 0.82),
      candidate(2, 3, 'event', 'Free Party Session', 0.73),
      candidate(2, 4, 'venue', 'La Zone Libre', 0.71),
      candidate(2, 5, 'city', 'Brussels', 0.84)
    ]
  },
  {
    id: '00000000-0000-4000-8000-000000000003',
    source: 'youtube',
    source_id: 'yt-23hz-ritual',
    canonical_url: 'https://www.youtube.com/watch?v=yt-23hz-ritual',
    title: '23HZ LIVESET @ RITUAL FLOOR',
    description: 'Recorded in Dresden, Germany. 16.05.2026',
    duration_seconds: 5290,
    published_at: '2026-05-16T18:00:00Z',
    set_score: 0.78,
    review_status: 'inbox',
    artist_names: ['23HZ'],
    event_name: 'Ritual Floor',
    venue: null,
    city: 'Dresden',
    year: 2026,
    primary_image_url: null,
    artwork_index: 2,
    raw_payload: { provider: 'youtube', channel: 'RITUAL GATHERING', tags: ['industrial', 'liveset'] },
    candidates: [
      candidate(3, 1, 'artist', '23HZ', 0.91),
      candidate(3, 2, 'event', 'Ritual Floor', 0.83),
      candidate(3, 3, 'city', 'Dresden', 0.84)
    ]
  },
  {
    id: '00000000-0000-4000-8000-000000000004',
    source: 'freeteknomusic',
    source_id: 'ftm-noisekraft',
    canonical_url: 'https://freeteknomusic.org/noisekraft-ground-pressure',
    title: 'NOISEKRAFT — GROUND PRESSURE LIVE MIX',
    description: 'Funktion-One outdoor recording, Netherlands.',
    duration_seconds: 3933,
    published_at: '2026-05-15T18:00:00Z',
    set_score: 0.56,
    review_status: 'inbox',
    artist_names: ['NOISEKRAFT'],
    event_name: 'Ground Pressure',
    venue: 'Funktion-One Outdoor',
    city: null,
    year: 2026,
    primary_image_url: null,
    artwork_index: 3,
    raw_payload: { provider: 'freeteknomusic', tags: ['tekno', 'mix'] },
    candidates: [
      candidate(4, 1, 'artist', 'NOISEKRAFT', 0.87),
      candidate(4, 2, 'event', 'Ground Pressure', 0.69)
    ]
  },
  {
    id: '00000000-0000-4000-8000-000000000005',
    source: 'soundcloud',
    source_id: 'sc-acid-assembly',
    canonical_url: 'https://soundcloud.com/syco23/acid-assembly',
    title: 'ACID ASSEMBLY — WAREHOUSE MIX',
    description: 'Long-form acid session from Prague.',
    duration_seconds: 4800,
    published_at: '2026-05-14T18:00:00Z',
    set_score: 0.74,
    review_status: 'accepted',
    artist_names: ['ACID ASSEMBLY'],
    event_name: 'Warehouse Session',
    city: 'Prague',
    primary_image_url: null,
    artwork_index: 0
  },
  {
    id: '00000000-0000-4000-8000-000000000006',
    source: 'youtube',
    source_id: 'yt-syco-transmission',
    canonical_url: 'https://www.youtube.com/watch?v=yt-syco-transmission',
    title: 'SYCO TRANSMISSION 023 — INDUSTRIAL TRIBE',
    description: 'Published SYSTEM CORRUPT transmission.',
    duration_seconds: 7320,
    published_at: '2026-05-13T18:00:00Z',
    set_score: 0.91,
    review_status: 'published',
    artist_names: ['SYCO'],
    event_name: 'Transmission 023',
    city: 'Berlin',
    primary_image_url: null,
    artwork_index: 3
  }
]

export const demoStats: Stats = {
  total_sets: 6,
  by_source: { youtube: 3, soundcloud: 2, freeteknomusic: 1 },
  by_status: { inbox: 4, reviewing: 0, accepted: 1, rejected: 0, published: 1 },
  score_bands: { high: 4, review: 2, low: 0 },
  queue: { queued: 7, processing: 3, failed: 2, completed: 18, retry: 1, blocked: 1 }
}

export const demoProviderHealth: ProviderHealthStatus = {
  youtube: { configured: true, enabled: true, mode: 'official_api' },
  soundcloud: { configured: true, enabled: true, mode: 'manual_url' },
  freeteknomusic: { configured: true, enabled: false, mode: 'robots_crawl' }
}

export const demoJobs: ImportJob[] = [
  { id: 'd0b00000-0000-4000-8000-000000000001', url: 'https://www.youtube.com/watch?v=syco23', source: 'youtube', job_type: 'url_import', profile_id: null, status: 'queued', attempt_count: 0, created_at: '2026-07-28T11:15:00Z', started_at: null, finished_at: null, next_retry_at: null, result_set_id: null, error_code: null, error_message: null, details: {} },
  { id: 'd0b00000-0000-4000-8000-000000000002', url: 'https://soundcloud.com/syco23/failed-set', source: 'soundcloud', job_type: 'url_import', profile_id: null, status: 'failed', attempt_count: 3, created_at: '2026-07-28T10:40:00Z', started_at: '2026-07-28T10:41:00Z', finished_at: '2026-07-28T10:41:30Z', next_retry_at: null, result_set_id: null, error_code: 'soundcloud_timeout', error_message: 'Metadata process exceeded its 30 second limit.', details: {} },
  { id: 'd0b00000-0000-4000-8000-000000000003', url: null, source: 'youtube', job_type: 'search_profile', profile_id: 'profile-1', status: 'processing', attempt_count: 1, created_at: '2026-07-28T09:00:00Z', started_at: '2026-07-28T09:00:05Z', finished_at: null, next_retry_at: null, result_set_id: null, error_code: null, error_message: null, details: {} },
  { id: 'd0b00000-0000-4000-8000-000000000004', url: 'https://freeteknomusic.org/noisekraft-ground-pressure', source: 'freeteknomusic', job_type: 'url_import', profile_id: null, status: 'dead_letter', attempt_count: 3, created_at: '2026-07-28T08:00:00Z', started_at: '2026-07-28T08:00:10Z', finished_at: '2026-07-28T08:12:00Z', next_retry_at: null, result_set_id: null, error_code: 'ftm_robots_denied', error_message: 'The source robots policy denied this page.', details: {} }
]

export const demoProfiles: SearchProfile[] = [
  {
    id: 'profile-1',
    name: 'Freetekno livesets',
    query: 'freetekno liveset',
    source: 'youtube',
    schedule_cron: '0 6 * * *',
    last_run_at: '2026-07-28T06:00:00Z',
    next_page_token: 'NEXT_PAGE_23',
    last_result_count: 12,
    last_error_code: null,
    latest_job_id: 'd0b00000-0000-4000-8000-000000000003',
    enabled: true
  },
  {
    id: 'profile-2',
    name: 'Tribe B2B',
    query: 'tribe b2b dj set',
    source: 'youtube',
    schedule_cron: '0 6 * * *',
    last_run_at: null,
    next_page_token: null,
    last_result_count: null,
    last_error_code: 'youtube_quota_exceeded',
    latest_job_id: 'd0b00000-0000-4000-8000-000000000001',
    enabled: true
  },
  {
    id: 'profile-3',
    name: 'Known crews',
    query: 'teknival recorded at',
    source: 'youtube',
    schedule_cron: '0 6 * * *',
    last_run_at: '2026-07-27T06:00:00Z',
    next_page_token: null,
    last_result_count: 0,
    last_error_code: 'provider_mode_fixture',
    latest_job_id: 'd0b00000-0000-4000-8000-000000000004',
    enabled: false
  }
]
