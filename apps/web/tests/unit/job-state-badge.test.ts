import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import JobStateBadge from '../../components/JobStateBadge.vue'
import type { ImportJobStatus } from '../../types'

describe('JobStateBadge', () => {
  it('renders every durable job state with its status class', () => {
    const states: ImportJobStatus[] = ['queued', 'processing', 'retry', 'completed', 'failed', 'blocked', 'dead_letter']
    for (const status of states) {
      const wrapper = mount(JobStateBadge, { props: { status } })
      expect(wrapper.text()).toContain(status.replace('_', ' '))
      expect(wrapper.classes()).toContain(`job-state--${status}`)
    }
  })
})
