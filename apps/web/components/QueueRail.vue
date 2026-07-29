<script setup lang="ts">
defineProps<{ queue: { queued: number; processing: number; failed: number } }>()
</script>

<template>
  <aside class="queue-rail" aria-label="Import queue">
    <p class="utility-label">Queue</p>
    <div class="queue-cell">
      <span>Queued</span>
      <strong>{{ String(queue.queued).padStart(2, '0') }}</strong>
      <AppIcon name="events" :size="24" />
    </div>
    <div class="queue-cell">
      <span>Processing</span>
      <strong>{{ String(queue.processing).padStart(2, '0') }}</strong>
      <AppIcon name="overview" :size="24" />
    </div>
    <div class="queue-cell queue-cell--failed">
      <span>Failed</span>
      <strong>{{ String(queue.failed).padStart(2, '0') }}</strong>
      <AppIcon name="close" :size="24" />
    </div>
    <div class="queue-totem" aria-hidden="true">
      <i /><i /><i />
      <b>SC23</b>
    </div>
  </aside>
</template>
