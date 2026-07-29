<script setup lang="ts">
import { demoSets } from '~/data/demo'
import type { SetPage } from '~/types'
import { fixtureValue } from '~/utils/runtime'

const route = useRoute()
const runtime = useSycoRuntime()
const { get } = useApi()
const { ready } = useAuth()
const query = computed(() => String(route.query.search || ''))
const fixturePage: SetPage = { items: demoSets, total: demoSets.length, limit: 50, offset: 0 }
const emptyPage: SetPage = { items: [], total: 0, limit: 50, offset: 0 }
const { data: page, error, execute: loadSets } = await useAsyncData<SetPage>('all-sets', () => get('/sets', fixturePage), { server: false, immediate: false, default: () => fixtureValue(runtime.runtimeMode, fixturePage, emptyPage) })
useOperationalLoad(ready, loadSets)
const visible = computed(() => {
  const needle = query.value.toLowerCase()
  return (page.value?.items || []).filter((set) => !needle || `${set.title} ${set.artist_names.join(' ')}`.toLowerCase().includes(needle))
})
</script>

<template>
  <section>
    <PageHeader title="Sets" :count="`${visible.length} METADATA RECORDS`" />
    <div v-if="query" class="active-query">Search result for “{{ query }}” <NuxtLink to="/sets">Clear</NuxtLink></div>
    <div class="set-list browse-list">
      <SetCard v-for="set in visible" :key="set.id" :set="set" />
      <div v-if="!visible.length" class="empty-state">No set records are available.</div>
    </div>
    <p v-if="error" class="form-message error" role="alert">Sets unavailable: {{ error.message }}</p>
  </section>
</template>
