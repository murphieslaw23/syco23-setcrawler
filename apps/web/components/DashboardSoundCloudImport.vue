<script setup lang="ts">
import { ref } from 'vue'

defineProps<{
  canEdit: boolean
  busy: boolean
  error: string
  jobId: string | null
}>()

const emit = defineEmits<{
  submit: [url: string]
}>()
const url = ref('')

function submit() {
  emit('submit', url.value)
}
</script>

<template>
  <form class="dashboard-import" @submit.prevent="submit">
    <label>
      SoundCloud track URL
      <input
        v-model="url"
        type="url"
        required
        :disabled="!canEdit || busy"
        placeholder="https://soundcloud.com/crew/live-set"
      >
    </label>
    <button class="primary-button" type="submit" :disabled="!canEdit || busy">
      {{ busy ? 'Queueing…' : canEdit ? 'Import SoundCloud metadata' : 'Editor access required' }}
    </button>
    <p v-if="error" class="form-message error" role="alert">{{ error }}</p>
    <p v-if="jobId" class="form-message" role="status">
      Job {{ jobId }} queued.
      <NuxtLink :to="`/imports#job-${jobId}`">Open job</NuxtLink>
    </p>
  </form>
</template>
