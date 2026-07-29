<script setup lang="ts">
import { demoSets } from '~/data/demo'
import type { SetPage, SetSource } from '~/types'
import { fixtureValue } from '~/utils/runtime'

const runtime = useSycoRuntime()
const { get } = useApi()
const { ready } = useAuth()
const source = ref<'all' | SetSource>('all')
const minScore = ref('0')
const search = ref('')
const fixturePage: SetPage = {
  items: demoSets.filter((set) => set.review_status === 'inbox'),
  total: 4,
  limit: 50,
  offset: 0
}
const emptyPage: SetPage = { items: [], total: 0, limit: 50, offset: 0 }
const { data: page, status, error, execute: loadInbox } = await useAsyncData<SetPage>('inbox-sets', () => get('/sets?status=inbox', fixturePage), {
  server: false,
  immediate: false,
  default: () => fixtureValue(runtime.runtimeMode, fixturePage, emptyPage)
})
useOperationalLoad(ready, loadInbox)
const filtered = computed(() => (page.value?.items || []).filter((set) => {
  const matchesSource = source.value === 'all' || set.source === source.value
  const matchesScore = set.set_score >= Number(minScore.value)
  const needle = search.value.trim().toLowerCase()
  const matchesSearch = !needle || `${set.title} ${set.artist_names.join(' ')} ${set.event_name || ''}`.toLowerCase().includes(needle)
  return matchesSource && matchesScore && matchesSearch
}))
</script>

<template>
  <section>
    <PageHeader title="Review Inbox" :count="`${filtered.length} SETS PENDING`" />
    <div class="filter-bar">
      <label class="filter-search">
        <AppIcon name="search" />
        <input v-model="search" type="search" placeholder="Search this inbox" aria-label="Search review inbox">
      </label>
      <label>Source
        <select v-model="source">
          <option value="all">All</option>
          <option value="youtube">YouTube</option>
          <option value="soundcloud">SoundCloud</option>
          <option value="freeteknomusic">FreeTeknoMusic</option>
        </select>
      </label>
      <label>Confidence
        <select v-model="minScore">
          <option value="0">Any</option>
          <option value="0.7">70%+</option>
          <option value="0.8">80%+</option>
        </select>
      </label>
      <button class="text-button" type="button" @click="source = 'all'; minScore = '0'; search = ''">Clear ×</button>
    </div>

    <div class="set-table-head" aria-hidden="true">
      <span>Set</span><span>Source</span><span>Duration</span><span>Published</span><span>Confidence</span><span>Status</span><span />
    </div>
    <div class="set-list" :aria-busy="status === 'pending'">
      <SetCard v-for="set in filtered" :key="set.id" :set="set" />
      <div v-if="!filtered.length" class="empty-state">No sets match the current filters.</div>
    </div>
    <p v-if="error" class="form-message error" role="alert">Inbox data is unavailable: {{ error.message }}</p>
    <footer class="list-footer">Showing {{ filtered.length }} of {{ page?.total || 0 }} review items</footer>
  </section>
</template>
