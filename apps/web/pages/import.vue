<script setup lang="ts">
const { send } = useApi()
const { canEdit } = useAuth()
const url = ref('')
const status = ref<'idle' | 'busy' | 'done' | 'error'>('idle')
const message = ref('')

async function submit() {
  status.value = 'busy'
  try {
    const parsed = new URL(url.value)
    const allowed = ['youtube.com', 'www.youtube.com', 'youtu.be', 'soundcloud.com', 'freeteknomusic.org', 'www.freeteknomusic.org']
    if (!allowed.includes(parsed.hostname) && !parsed.hostname.endsWith('.soundcloud.com')) throw new Error('unsupported')
    if (!canEdit.value) throw new Error('Your viewer role cannot queue imports.')
    const job = await send<{ id: string }>('/imports/url', 'POST', { url: url.value })
    status.value = 'done'
    message.value = `Metadata import queued as ${job.id}. No audio or video will be downloaded.`
    url.value = ''
  } catch (error) {
    status.value = 'error'
    message.value = error instanceof Error && error.message !== 'unsupported' ? error.message : 'Use a valid YouTube, SoundCloud, or freeteknomusic.org URL.'
  }
}
</script>

<template>
  <section class="import-page">
    <PageHeader title="Import URL" count="MANUAL METADATA INTAKE" />
    <form class="plate import-panel" @submit.prevent="submit">
      <div class="import-mark"><AppIcon name="import" :size="36" /></div>
      <p class="utility-label">One source URL</p>
      <h2>Queue a liveset for review</h2>
      <p>SoundCloud is manual-only. YouTube and FreeTeknoMusic URLs are accepted for local workflow testing.</p>
      <label>
        Provider URL
        <input v-model="url" type="url" required placeholder="https://soundcloud.com/crew/live-set">
      </label>
      <button class="primary-button" type="submit" :disabled="status === 'busy' || !canEdit">{{ status === 'busy' ? 'Validating…' : canEdit ? 'Queue metadata import' : 'Editor access required' }}</button>
      <p v-if="message" class="form-message" :class="{ error: status === 'error' }" role="status">{{ message }}</p>
      <NuxtLink v-if="status === 'done'" class="secondary-button" to="/imports">Watch import queue</NuxtLink>
    </form>
  </section>
</template>
