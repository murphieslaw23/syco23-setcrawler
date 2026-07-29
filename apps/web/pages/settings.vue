<script setup lang="ts">
const { isAdmin } = useAuth()
const minimum = ref(20)
const review = ref(40)
const automatic = ref(70)
const strong = ref('liveset, live set, dj set, b2b, teknival, free party, freetekno, mix')
const negative = ref('official video, music video, single, EP, album, tutorial, review')
const saved = ref(false)
function save() {
  if (!isAdmin.value) return
  saved.value = true
  window.setTimeout(() => { saved.value = false }, 2500)
}
</script>

<template>
  <section>
    <PageHeader title="Settings" count="HEURISTIC CONFIGURATION" />
    <form v-if="isAdmin" class="settings-grid" @submit.prevent="save">
      <section class="plate">
        <p class="utility-label">Duration gate</p><h2>Set thresholds</h2>
        <label>Minimum duration <span>{{ minimum }} min</span><input v-model="minimum" type="range" min="10" max="60"></label>
        <label>Review threshold <span>{{ review }}%</span><input v-model="review" type="range" min="0" max="100"></label>
        <label>High-confidence marker <span>{{ automatic }}%</span><input v-model="automatic" type="range" min="40" max="100"></label>
        <p class="notice">High confidence never publishes automatically. A human editor must publish every set explicitly.</p>
      </section>
      <section class="plate">
        <p class="utility-label">Signal lists</p><h2>Keyword weights</h2>
        <label>Strong signals<textarea v-model="strong" rows="5" /></label>
        <label>Negative signals<textarea v-model="negative" rows="5" /></label>
        <div class="form-actions"><span v-if="saved" role="status">Saved locally.</span><button class="primary-button" type="submit">Save configuration</button></div>
      </section>
    </form>
    <section v-else class="plate access-note"><p class="utility-label">Read only</p><h2>Administrator access required</h2><p>Heuristic configuration is available to administrators only.</p></section>
  </section>
</template>
