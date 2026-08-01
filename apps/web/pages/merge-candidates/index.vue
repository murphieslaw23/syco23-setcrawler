<script setup lang="ts">
import type {
  MergeCandidate,
  MergeCandidatePage,
  MergeCandidateStatus,
  SetProviderSource,
  SetRecord
} from '~/types'
import { formatDate, formatDuration, formatScore } from '~/utils/format'

interface MergeReview {
  candidate: MergeCandidate
  source: SetRecord
  target: SetRecord
  sourceSources: SetProviderSource[]
  targetSources: SetProviderSource[]
}

const { get, send } = useApi()
const { ready, isAdmin } = useAuth()
const statusFilter = ref<MergeCandidateStatus>('pending')
const busyCandidate = ref<string | null>(null)
const actionMessage = ref('')
const adminReady = computed(() => ready.value && isAdmin.value)
const emptyPage: MergeCandidatePage = {
  items: [],
  total: 0,
  limit: 50,
  offset: 0
}

async function loadReviews(): Promise<MergeReview[]> {
  const page = await get<MergeCandidatePage>(
    `/merge-candidates?status=${statusFilter.value}`,
    emptyPage
  )
  return await Promise.all(page.items.map(async (candidate) => {
    const [source, target, sourceSources, targetSources] = await Promise.all([
      get<SetRecord>(`/sets/${candidate.source_set_id}`),
      get<SetRecord>(`/sets/${candidate.target_set_id}`),
      get<SetProviderSource[]>(`/sets/${candidate.source_set_id}/sources`, []),
      get<SetProviderSource[]>(`/sets/${candidate.target_set_id}/sources`, [])
    ])
    return { candidate, source, target, sourceSources, targetSources }
  }))
}

const {
  data: reviews,
  status,
  error,
  execute: refreshReviews
} = await useAsyncData<MergeReview[]>(
  'canonical-merge-review',
  loadReviews,
  { server: false, immediate: false, default: () => [] }
)
useOperationalLoad(adminReady, refreshReviews)
watch(statusFilter, () => {
  if (adminReady.value) void refreshReviews()
})

async function decide(
  candidate: MergeCandidate,
  action: 'approve' | 'reject' | 'restore'
) {
  if (!isAdmin.value) {
    actionMessage.value = 'Administrator access is required for merge decisions.'
    return
  }
  busyCandidate.value = candidate.id
  actionMessage.value = ''
  try {
    await send<MergeCandidate>(
      `/merge-candidates/${candidate.id}/${action}`,
      'POST'
    )
    actionMessage.value = action === 'approve'
      ? 'Sources linked to the canonical set. The original record can be restored.'
      : action === 'reject'
        ? 'The records will remain separate.'
        : 'The original set and its sources were restored.'
    await refreshReviews()
  } catch (decisionError) {
    actionMessage.value = decisionError instanceof Error
      ? decisionError.message
      : 'The merge decision could not be saved.'
  } finally {
    busyCandidate.value = null
  }
}

function componentValues(candidate: MergeCandidate) {
  return [
    ['Title & artist', candidate.component_scores.title_artist],
    ['Event', candidate.component_scores.event],
    ['Date / year', candidate.component_scores.date_year],
    ['Duration', candidate.component_scores.duration],
    ['Aliases', candidate.component_scores.aliases]
  ] as const
}
</script>

<template>
  <section>
    <PageHeader title="Canonical Merge Review" :count="`${reviews?.length || 0} SUGGESTIONS`" />

    <div v-if="!isAdmin" class="notice">
      Administrator access is required to review canonical-set suggestions.
    </div>

    <template v-else>
      <div class="filter-bar merge-filter">
        <label>Status
          <select v-model="statusFilter">
            <option value="pending">Pending review</option>
            <option value="approved">Approved</option>
            <option value="rejected">Kept separate</option>
            <option value="restored">Restored</option>
          </select>
        </label>
        <p v-if="actionMessage" role="status">{{ actionMessage }}</p>
      </div>

      <div class="merge-review-list" :aria-busy="status === 'pending'">
        <article v-for="review in reviews" :key="review.candidate.id" class="plate merge-review-card">
          <header class="merge-review-card__header">
            <div>
              <p class="utility-label">Human decision required</p>
              <h2>{{ formatScore(review.candidate.score) }} overall match</h2>
            </div>
            <span class="status-label">{{ review.candidate.status }}</span>
          </header>

          <div class="merge-pair">
            <section class="merge-set-panel">
              <p class="utility-label">Incoming source record</p>
              <SourceBadge :source="review.source.source" />
              <h3>{{ review.source.title }}</h3>
              <p>{{ review.source.artist_names.join(' · ') || 'Artist unresolved' }}</p>
              <dl>
                <div><dt>Event</dt><dd>{{ review.source.event_name || 'Unresolved' }}</dd></div>
                <div><dt>Date</dt><dd>{{ formatDate(review.source.published_at) }}</dd></div>
                <div><dt>Duration</dt><dd>{{ formatDuration(review.source.duration_seconds) }}</dd></div>
              </dl>
              <ul class="merge-source-list">
                <li v-for="item in review.sourceSources" :key="`${item.provider_key}:${item.external_id}`">
                  <a :href="item.canonical_url" target="_blank" rel="noopener noreferrer">{{ item.provider_key }} · {{ item.external_id }} ↗</a>
                </li>
              </ul>
            </section>

            <section class="merge-set-panel merge-set-panel--target">
              <p class="utility-label">Canonical target</p>
              <SourceBadge :source="review.target.source" />
              <h3>{{ review.target.title }}</h3>
              <p>{{ review.target.artist_names.join(' · ') || 'Artist unresolved' }}</p>
              <dl>
                <div><dt>Event</dt><dd>{{ review.target.event_name || 'Unresolved' }}</dd></div>
                <div><dt>Date</dt><dd>{{ formatDate(review.target.published_at) }}</dd></div>
                <div><dt>Duration</dt><dd>{{ formatDuration(review.target.duration_seconds) }}</dd></div>
              </dl>
              <ul class="merge-source-list">
                <li v-for="item in review.targetSources" :key="`${item.provider_key}:${item.external_id}`">
                  <a :href="item.canonical_url" target="_blank" rel="noopener noreferrer">{{ item.provider_key }} · {{ item.external_id }} ↗</a>
                </li>
              </ul>
            </section>
          </div>

          <section class="merge-score-evidence">
            <div
              v-for="([label, value]) in componentValues(review.candidate)"
              :key="label"
              class="merge-score-evidence__item"
            >
              <span>{{ label }}</span>
              <b>{{ formatScore(value) }}</b>
              <i><span :style="{ width: formatScore(value) }" /></i>
            </div>
            <p>{{ review.candidate.reasons.join(' · ') || 'No strong component reason' }}</p>
          </section>

          <footer class="merge-review-actions">
            <template v-if="review.candidate.status === 'pending'">
              <button
                class="secondary-button"
                type="button"
                :disabled="busyCandidate === review.candidate.id"
                @click="decide(review.candidate, 'reject')"
              >Keep separate</button>
              <button
                class="primary-button"
                type="button"
                :disabled="busyCandidate === review.candidate.id"
                @click="decide(review.candidate, 'approve')"
              >Approve merge</button>
            </template>
            <button
              v-else-if="review.candidate.status === 'approved'"
              class="secondary-button"
              type="button"
              :disabled="busyCandidate === review.candidate.id"
              @click="decide(review.candidate, 'restore')"
            >Restore merge</button>
          </footer>
        </article>

        <div v-if="!reviews?.length && status !== 'pending'" class="empty-state">
          No merge suggestions have this status.
        </div>
      </div>
      <p v-if="error" class="form-message error" role="alert">Merge review is unavailable: {{ error.message }}</p>
    </template>
  </section>
</template>
