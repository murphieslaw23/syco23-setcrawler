<script setup lang="ts">
const { signInWithEmail } = useAuth()
const email = ref('')
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')

async function submit() {
  busy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await signInWithEmail(email.value.trim())
    message.value = 'Magic link sent. Check your inbox, then return to SYCO23.'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Sign-in could not be started.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="login-page">
    <PageHeader title="Sign in" count="EDITORIAL ACCESS" />
    <form class="plate login-panel" @submit.prevent="submit">
      <p class="utility-label">System corrupt access</p>
      <h2>Enter the review floor</h2>
      <p>Use your approved editorial email. Access is issued with a one-time link.</p>
      <label>Email address<input v-model="email" type="email" autocomplete="email" required placeholder="you@example.com"></label>
      <button class="primary-button" type="submit" :disabled="busy">{{ busy ? 'Sending…' : 'Send magic link' }}</button>
      <p v-if="message" class="form-message" role="status">{{ message }}</p>
      <p v-if="errorMessage" class="form-message error" role="alert">{{ errorMessage }}</p>
    </form>
  </section>
</template>
