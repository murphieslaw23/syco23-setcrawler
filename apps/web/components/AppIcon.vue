<script setup lang="ts">
defineProps<{ name: string; size?: number }>()
</script>

<template>
  <svg
    class="app-icon"
    :width="size || 20"
    :height="size || 20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="square"
    stroke-linejoin="miter"
    aria-hidden="true"
  >
    <template v-if="name === 'overview'">
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="2" />
    </template>
    <template v-else-if="name === 'inbox'">
      <path d="M4 6h16v13H4zM4 14h5l2 2h2l2-2h5" />
    </template>
    <template v-else-if="name === 'sets'">
      <path d="m12 3 8 4-8 4-8-4 8-4Zm-8 9 8 4 8-4M4 17l8 4 8-4" />
    </template>
    <template v-else-if="name === 'artists'">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21c1-5 4-7 8-7s7 2 8 7" />
    </template>
    <template v-else-if="name === 'events'">
      <path d="M4 6h16v15H4zM8 3v6m8-6v6M4 11h16" />
    </template>
    <template v-else-if="name === 'search'">
      <circle cx="10" cy="10" r="6" />
      <path d="m15 15 5 5" />
    </template>
    <template v-else-if="name === 'settings'">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M18.4 5.6l-2.1 2.1m-8.6 8.6-2.1 2.1" />
    </template>
    <template v-else-if="name === 'import'">
      <path d="M12 3v12m-5-5 5 5 5-5M4 20h16" />
    </template>
    <template v-else-if="name === 'filter'">
      <path d="M4 5h16l-6 7v6l-4 2v-8L4 5Z" />
    </template>
    <template v-else-if="name === 'arrow'">
      <path d="M5 12h14m-5-5 5 5-5 5" />
    </template>
    <template v-else-if="name === 'close'">
      <path d="m6 6 12 12M18 6 6 18" />
    </template>
    <template v-else>
      <path d="M5 5h14v14H5z" />
    </template>
  </svg>
</template>
