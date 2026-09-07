import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import Pager from '../../src/ohmymeme/presentation/frontend/main/features/memes/Pager.vue'

describe('Pager', () => {
  it('emits the next page when the next button is clicked', async () => {
    const wrapper = mount(Pager, {
      props: { page: 3, pageCount: 8 },
    })

    await wrapper.get('[title="下一页"]').trigger('click')

    expect(wrapper.emitted('go')).toEqual([[4]])
  })

  it('shows gaps between non-adjacent page choices', () => {
    const wrapper = mount(Pager, {
      props: { page: 5, pageCount: 10 },
    })

    expect(wrapper.findAll('.pager-dots')).toHaveLength(2)
  })
})
