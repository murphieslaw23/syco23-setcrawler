create index sets_review_status_idx on sets (review_status);
create index sets_source_idx on sets (source);
create index sets_score_idx on sets (set_score desc);
create index sets_published_at_idx on sets (published_at desc);
create index sets_title_search_idx on sets using gin (to_tsvector('simple', title));
create index field_candidates_set_idx on field_candidates (set_id, accepted);
create index set_images_priority_idx on set_images (set_id, is_primary desc, priority desc);
create index images_phash_idx on images (perceptual_hash) where perceptual_hash is not null;
create index search_profiles_enabled_idx on search_profiles (enabled) where enabled = true;
