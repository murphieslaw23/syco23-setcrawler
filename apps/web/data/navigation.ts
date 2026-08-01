export const navigation = [
  { label: 'Overview', to: '/', icon: 'overview' },
  { label: 'Review Inbox', to: '/inbox', icon: 'inbox' },
  { label: 'Merge Review', to: '/merge-candidates', icon: 'sets' },
  { label: 'Imports', to: '/imports', icon: 'import' },
  { label: 'Sets', to: '/sets', icon: 'sets' },
  { label: 'Artists', to: '/artists', icon: 'artists' },
  { label: 'Events', to: '/events', icon: 'events' },
  { label: 'Search Profiles', to: '/search-profiles', icon: 'search' },
  { label: 'Settings', to: '/settings', icon: 'settings' }
] as const

export const mobileNavigation = [
  { label: 'Overview', to: '/', icon: 'overview' },
  { label: 'Inbox', to: '/inbox', icon: 'inbox' },
  { label: 'Imports', to: '/imports', icon: 'import' },
  { label: 'Sets', to: '/sets', icon: 'sets' }
] as const
