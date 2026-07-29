<script setup lang="ts">
import { demoSets } from '~/data/demo'
import type { Candidate, SetRecord } from '~/types'
import { formatDate, formatDuration, formatScore } from '~/utils/format'
import { canChangeSetStatus } from '~/utils/auth'
import { fixtureValue } from '~/utils/runtime'

const route = useRoute()
const router = useRouter()
const config = useRuntimeConfig()
const { get, send } = useApi()
const { ready, role, canEdit, isAdmin } = useAuth()
const fixtureRecord = demoSets.find((set) => set.id === route.params.id) || demoSets[0]!
const { data: record, error, status, execute: loadRecord } = await useAsyncData<SetRecord | null>(`set-${route.params.id}`, () => get<SetRecord>(`/sets/${route.params.id}`, fixtureRecord), {
  server: false,
  immediate: false,
  default: () => fixtureValue<SetRecord | null>(config.public.runtimeMode as string, fixtureRecord, null)
})
useOperationalLoad(ready, loadRecord)
const busyCandidate = ref<string | null>(null)
const actionMessage = ref('')

async function decide(candidate: Candidate, accepted: boolean) {
  if (!record.value || !canEdit.value) {
    actionMessage.value = 'Editor access is required to review candidates.'
    return
  }
  busyCandidate.value = candidate.id
  try {
    const result = await send<Candidate>(
      `/sets/${record.value.id}/candidates/${candidate.id}/${accepted ? 'accept' : 'reject'}`,
      'POST'
    )
    Object.assign(candidate, result)
  } catch (error) {
    actionMessage.value = error instanceof Error ? error.message : 'Candidate decision could not be saved.'
  } finally {
    busyCandidate.value = null
  }
}

async function setStatus(status: 'accepted' | 'rejected' | 'published') {
  const permitted = canChangeSetStatus(role.value, status)
  if (!record.value || !permitted) {
    actionMessage.value = status === 'published' ? 'Administrator access is required to publish a set.' : 'Editor access is required to change set status.'
    return
  }
  try {
    const path = status === 'published' ? `/sets/${record.value.id}/publish` : status === 'rejected' ? `/sets/${record.value.id}/reject` : `/sets/${record.value.id}`
    const updated = await send<SetRecord>(path, status === 'accepted' ? 'PATCH' : 'POST', status === 'accepted' ? { review_status: status } : undefined)
    record.value = { ...record.value, ...updated }
  } catch (error) {
    actionMessage.value = error instanceof Error ? error.message : 'Set status could not be changed.'
    return
  }
  actionMessage.value = `Status changed to ${status}.`
}
</script>

<template>
  <section v-if="record" class="review-detail">
    <div class="detail-toolbar">
      <button class="back-button" type="button" @click="router.push('/inbox')">← Review inbox</button>
      <span class="status-label">{{ record.review_status }}</span>
    </div>
    <div class="detail-hero plate">
      <SetArtwork :index="record.artwork_index" :alt="`${record.title} artwork`" />
      <div class="detail-hero__content">
        <SourceBadge :source="record.source" />
        <h1>{{ record.title }}</h1>
        <p>{{ record.artist_names.join(' · ') }}<span v-if="record.event_name"> @ {{ record.event_name }}</span></p>
        <dl>
          <div><dt>Duration</dt><dd>{{ formatDuration(record.duration_seconds) }}</dd></div>
          <div><dt>Published</dt><dd>{{ formatDate(record.published_at) }}</dd></div>
          <div><dt>Confidence</dt><dd>{{ formatScore(record.set_score) }}</dd></div>
          <div><dt>Location</dt><dd>{{ [record.venue, record.city].filter(Boolean).join(', ') || 'Unresolved' }}</dd></div>
        </dl>
      </div>
      <ScoreBar :score="record.set_score" />
    </div>

    <div class="review-columns">
      <section class="plate candidate-panel">
        <div class="section-heading">
          <div><p class="utility-label">Human verification</p><h2>Field candidates</h2></div>
          <span>{{ record.candidates?.length || 0 }} extracted</span>
        </div>
        <CandidateField
          v-for="item in record.candidates"
          :key="item.id"
          :candidate="item"
          :busy="busyCandidate === item.id"
          :editable="canEdit"
          @accept="decide(item, true)"
          @reject="decide(item, false)"
        />
      </section>
      <section class="plate raw-panel">
        <p class="utility-label">Provider evidence</p>
        <h2>Raw data</h2>
        <dl>
          <div><dt>Title</dt><dd>{{ record.title }}</dd></div>
          <div><dt>Description</dt><dd>{{ record.description }}</dd></div>
          <div><dt>Source ID</dt><dd>{{ record.source_id }}</dd></div>
          <div><dt>Payload</dt><dd>{{ JSON.stringify(record.raw_payload, null, 2) }}</dd></div>
          <div v-if="record.import_job_id"><dt>Import job</dt><dd><NuxtLink :to="`/imports?job=${record.import_job_id}`">{{ record.import_job_id }}</NuxtLink></dd></div>
          <div v-if="record.score_reasons?.length"><dt>Score reasons</dt><dd>{{ record.score_reasons.join(' · ') }}</dd></div>
        </dl>
        <a class="secondary-button" :href="record.canonical_url" target="_blank" rel="noopener noreferrer">View on source ↗</a>
      </section>
    </div>

    <div v-if="canEdit" class="review-actions">
      <p v-if="actionMessage" role="status">{{ actionMessage }}</p>
      <button class="danger-button" type="button" @click="setStatus('rejected')">Reject set</button>
      <button class="secondary-button" type="button" @click="setStatus('accepted')">Accept for curation</button>
      <button v-if="isAdmin && (record.review_status === 'accepted' || record.review_status === 'reviewing')" class="primary-button" type="button" @click="setStatus('published')">Publish explicitly</button>
    </div>
    <p v-else class="notice">Viewer access is read-only. Candidate and publishing actions are unavailable.</p>
  </section>
  <section v-else class="plate empty-state" :aria-busy="status === 'pending'">
    <p v-if="status === 'pending'">Loading editorial record…</p>
    <p v-else-if="error" role="alert">This set could not be loaded: {{ error.message }}</p>
    <p v-else>No editorial record is available.</p>
  </section>
</template>
