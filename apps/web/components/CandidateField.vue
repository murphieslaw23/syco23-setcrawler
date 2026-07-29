<script setup lang="ts">
import type { Candidate } from '~/types'
import { formatScore } from '~/utils/format'

defineProps<{ candidate: Candidate; busy?: boolean; editable?: boolean }>()
const emit = defineEmits<{ accept: []; reject: [] }>()
</script>

<template>
  <div class="candidate" :class="{ 'candidate--accepted': candidate.accepted === true, 'candidate--rejected': candidate.accepted === false }">
    <div>
      <span class="candidate__field">{{ candidate.field_name }}</span>
      <strong>{{ candidate.candidate_value }}</strong>
      <small>{{ candidate.source.replace('_', ' ') }} · {{ formatScore(candidate.confidence) }}</small>
    </div>
    <div v-if="editable" class="candidate__actions">
      <button :disabled="busy" aria-label="Accept candidate" @click="emit('accept')">✓</button>
      <button :disabled="busy" aria-label="Reject candidate" @click="emit('reject')">×</button>
    </div>
  </div>
</template>
