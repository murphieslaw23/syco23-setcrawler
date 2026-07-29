<script setup lang="ts">
import { demoSets } from '~/data/demo'
import { fixtureValue } from '~/utils/runtime'

const runtime = useSycoRuntime()
const availableSets = fixtureValue(runtime.runtimeMode, demoSets, [])
const artists = computed(() => {
  const map = new Map<string, { name: string; sets: number; latest: string }>()
  for (const set of availableSets) {
    for (const name of set.artist_names) {
      const item = map.get(name) || { name, sets: 0, latest: set.published_at }
      item.sets += 1
      if (set.published_at > item.latest) item.latest = set.published_at
      map.set(name, item)
    }
  }
  return [...map.values()]
})
</script>

<template>
  <section>
    <PageHeader title="Artists" :count="`${artists.length} RESOLVED IDENTITIES`" />
    <div class="directory-grid">
      <article v-for="(artist, index) in artists" :key="artist.name" class="directory-card">
        <SetArtwork :index="index % 4" :alt="`${artist.name} visual`" />
        <div><span class="utility-label">Artist</span><h2>{{ artist.name }}</h2><p>{{ artist.sets }} linked set{{ artist.sets === 1 ? '' : 's' }}</p></div>
        <AppIcon name="arrow" />
      </article>
    </div>
    <p v-if="!artists.length" class="empty-state">Artist records are not available in this runtime.</p>
  </section>
</template>
