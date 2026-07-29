-- Private metadata-artwork buckets. Access remains denied until the image
-- pipeline adds narrowly scoped storage.objects policies or uses service_role.
insert into storage.buckets (
  id,
  name,
  public,
  file_size_limit,
  allowed_mime_types
)
values
  (
    'flyers',
    'flyers',
    false,
    20971520,
    array['image/jpeg', 'image/png', 'image/webp']
  ),
  (
    'thumbnails',
    'thumbnails',
    false,
    20971520,
    array['image/jpeg', 'image/png', 'image/webp']
  ),
  (
    'artist-images',
    'artist-images',
    false,
    20971520,
    array['image/jpeg', 'image/png', 'image/webp']
  )
on conflict (id) do update
set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;
