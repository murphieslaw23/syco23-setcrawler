<script setup lang="ts">
import type { ImportJobStatus } from '~/types'

defineProps<{ status: ImportJobStatus }>()
</script>

<template>
  <span class="job-state" :class="`job-state--${status}`">{{ status.replace('_', ' ') }}</span>
</template>
