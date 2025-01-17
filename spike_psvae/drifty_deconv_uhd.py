# %%
# %%
import h5py
import numpy as np
import tempfile
from pathlib import Path
from tqdm.auto import tqdm
import multiprocessing
from . import deconvolve, snr_templates, spike_train_utils, reassignment
from .waveform_utils import get_pitch, pitch_shift_templates
from .extract_deconv import extract_deconv


# %%
def superres_spike_train(
    spike_train, z_abs, x, bin_size_um, geom, t_end=100, 
    units_spread=None, n_spikes_max_recent = 1000, fs=30000, 
    dist_metric=None, dist_metric_threshold=500,
    adaptive_th_for_temp_computation=False, outliers_tracking=None,
):
    """
    remove min_spikes_bin by default - it's worse to end up with a template that is mean of all spikes!!!
    units_spread is the spread of each registered clusters np.std(z_reg[spt[:, 1]==unit])*1.65 
    """
    assert spike_train.shape == (*z_abs.shape, 2)
    assert bin_size_um > 0
    
    spike_train_no_outliers = spike_train.copy()
    if dist_metric is not None:
        # Remove outliers before computing templates
        spike_train_no_outliers[dist_metric<dist_metric_threshold, 1]=-1
    if adaptive_th_for_temp_computation:
        spike_train_no_outliers[~outliers_tracking]=-1


    # bin the spikes to create a binned "superres spike train"
    # we'll use this spike train in an expanded label space to compute templates
    superres_labels = np.full_like(spike_train_no_outliers[:, 1], -1)
    # this will keep track of which superres template corresponds to which bin,
    # information which we will need later to determine how to shift the templates
    n_spikes_per_bin = []
    superres_label_to_bin_id = []
    superres_label_to_orig_label = []
    unit_max_channels = []
    unit_labels = np.unique(spike_train_no_outliers[spike_train_no_outliers[:, 1] >= 0, 1])
    medians_at_computation = np.zeros(unit_labels.max()+1)
    cur_superres_label = 0
    for u in unit_labels:
        in_u = np.flatnonzero(spike_train_no_outliers[:, 1] == u)

        # Get most recent spikes
        count_unit = np.logical_and(spike_train_no_outliers[:, 0]<t_end*fs, spike_train_no_outliers[:, 1]==u).sum()
        if count_unit>n_spikes_max_recent:
            in_u = np.flatnonzero(np.logical_and(spike_train_no_outliers[:, 0]<t_end*fs, 
                                                           spike_train_no_outliers[:, 1]==u))[-n_spikes_max_recent:]
        else:
            in_u = np.flatnonzero(spike_train_no_outliers[:, 1]==u)[:n_spikes_max_recent]

        # center the z positions in this unit using the median
        centered_z = z_abs[in_u].copy()
        medians_at_computation[u] = np.median(centered_z)
        centered_z -= medians_at_computation[u]

        # convert them to bin identities by adding half the bin size and
        # floor dividing by the bin size
        # this corresponds to bins like:
        #      ... | bin -1 | bin 0 | bin 1 | ...
        #   ... -3bin/2 , -bin/2, bin/2, 3bin/2, ...
        bin_ids = (centered_z + bin_size_um / 2) // bin_size_um
        occupied_bins, bin_counts = np.unique(bin_ids, return_counts=True)
        if units_spread is not None:
            # np.abs(bin_ids) <= (np.abs(centered_z)+ bin_size_um / 2)//bin_size_um <= (max_z_dist + bin_size_um / 2)//bin_size_um
            bin_counts = bin_counts[
                np.abs(occupied_bins)
                <= (units_spread[u] + bin_size_um / 2) // bin_size_um
            ]
            occupied_bins = occupied_bins[
                np.abs(occupied_bins)
                <= (units_spread[u] + bin_size_um / 2) // bin_size_um
            ]
        # IS THAT NEEDED - min_spikes_bin removed here and used during template augmentation
#         if min_spikes_bin is None:
        for j, bin_id in enumerate(occupied_bins):
            superres_labels[in_u[bin_ids == bin_id]] = cur_superres_label
            superres_label_to_bin_id.append(bin_id)
            unit_max_channels.append(np.sum((geom - [np.median(x[in_u[bin_ids == bin_id]]), np.median(z_abs[in_u[bin_ids == bin_id]])])**2, axis=1).argmin())
            superres_label_to_orig_label.append(u)
            n_spikes_per_bin.append(bin_counts[j])
            cur_superres_label += 1
#         else:
#             if bin_counts.max() >= min_spikes_bin:
#                 for j, bin_id in enumerate(occupied_bins[bin_counts >= min_spikes_bin]):
#                     superres_labels[in_u[bin_ids == bin_id]] = cur_superres_label
#                     superres_label_to_bin_id.append(bin_id)
#                     superres_label_to_orig_label.append(u)
#                     n_spikes_per_bin.append(bin_counts[j])
#                     unit_max_channels.append(np.sum((geom - [np.median(x[in_u[bin_ids == bin_id]]), np.median(z_abs[in_u[bin_ids == bin_id]])])**2, axis=1).argmin())
#                     cur_superres_label += 1
#             # what if no template was computed for u
#             else:
#                 superres_labels[in_u] = cur_superres_label
#                 superres_label_to_bin_id.append(0)
#                 superres_label_to_orig_label.append(u)
#                 n_spikes_per_bin.append(in_u.shape[0])
#                 unit_max_channels.append(np.sum((geom - [np.median(x[in_u]), np.median(z_abs[in_u])])**2, axis=1).argmin())
#                 cur_superres_label += 1

    superres_label_to_bin_id = np.array(superres_label_to_bin_id)
    superres_label_to_orig_label = np.array(superres_label_to_orig_label)
    n_spikes_per_bin = np.array(n_spikes_per_bin)
    unit_max_channels = np.array(unit_max_channels)
    return (
        superres_labels,
        superres_label_to_bin_id,
        superres_label_to_orig_label,
        medians_at_computation,
        unit_max_channels,
        n_spikes_per_bin
    )


# %%

# %%
def superres_denoised_templates(
    spike_train,
    z_abs,
    x,
    bin_size_um,
    geom,
    raw_binary_file,
    t_end=100,
    min_spikes_bin=None,
    augment_low_snr_temps=True,
    min_spikes_to_augment=25,
    units_spread=None,
    dist_metric=None,
    dist_metric_threshold=1000,
    adaptive_th_for_temp_computation=False,
    outliers_tracking=None,
    max_spikes_per_unit=200, #per superres unit 
    n_spikes_max_recent = 1000,
    denoise_templates=True,
    do_temporal_decrease=True,
    zero_radius_um=70, #reduce this value in uhd compared to NP1/NP2
    reducer=np.mean,
    snr_threshold=5.0 * np.sqrt(100),
    spike_length_samples=121,
    trough_offset=42,
    do_tpca=True,
    tpca=None,
    tpca_rank=5,
    tpca_radius=75,
    tpca_n_wfs=50_000,
    tpca_centered=True,
    do_nn_denoise=False,
    denoiser_init_kwargs={}, 
    denoiser_weights_path=None, 
    device=None,
    batch_size=1024,
    fs=30000,
    pbar=True,
    seed=0,
    n_jobs=-1,
):

    (
        superres_labels,
        superres_label_to_bin_id,
        superres_label_to_orig_label,
        medians_at_computation,
        unit_max_channels,
        n_spikes_per_bin
    ) = superres_spike_train(
        spike_train,
        z_abs,
        x,
        bin_size_um,
        geom,
        t_end,
        units_spread,
        n_spikes_max_recent,
        fs,
        dist_metric,
        dist_metric_threshold,
        adaptive_th_for_temp_computation,
        outliers_tracking,
    )

    templates, extra = snr_templates.get_templates(
        np.c_[spike_train[:, 0], superres_labels],
        geom,
        raw_binary_file,
        unit_max_channels=unit_max_channels,
        max_spikes_per_unit=max_spikes_per_unit,
        do_temporal_decrease=do_temporal_decrease,
        zero_radius_um=zero_radius_um,
        reducer=reducer,
        snr_threshold=snr_threshold,
        spike_length_samples=spike_length_samples,
        trough_offset=trough_offset,
        do_tpca=do_tpca,
        tpca=tpca,
        tpca_rank=tpca_rank,
        tpca_radius=tpca_radius,
        tpca_n_wfs=tpca_n_wfs,
        tpca_centered=tpca_centered,
        use_previous_max_channels=True,
        do_nn_denoise=do_nn_denoise,
        denoiser_init_kwargs=denoiser_init_kwargs, 
        denoiser_weights_path=denoiser_weights_path, 
        device=device,
        batch_size=batch_size,
        pbar=pbar,
        seed=seed,
        n_jobs=n_jobs,
        raw_only=not denoise_templates,
    )
    
    if augment_low_snr_temps:
        templates, n_spikes_per_bin = augment_low_snr_templates(
                        templates, 
                        superres_label_to_bin_id,
                        superres_label_to_orig_label,
                        n_spikes_per_bin, 
                        bin_size_um,
                        geom,
                        min_spikes_to_augment = min_spikes_to_augment
        )

    if min_spikes_bin is not None:
        templates[n_spikes_per_bin<min_spikes_bin]=0
    
    return (
        templates,
        superres_label_to_bin_id,
        superres_label_to_orig_label,
        medians_at_computation
    )


# %%
def augment_low_snr_templates(
    superres_templates, 
    superres_label_to_bin_id,
    superres_label_to_orig_label,
    n_spikes_per_bin, 
    bin_size_um,
    geom,
    min_spikes_to_augment = 25,
    fill_value=0.0,
):
    
    pitch = get_pitch(geom)
    n_chans_per_row = (geom[:, 1]<pitch).sum()
    
    temp_to_augment = np.flatnonzero(n_spikes_per_bin<min_spikes_to_augment)
    for k in temp_to_augment:
        temp_orig = superres_label_to_orig_label[k]
        bin_id = superres_label_to_bin_id[k] 
        bins_to_augment = np.array([bin_id+pitch//bin_size_um, bin_id-pitch//bin_size_um])
        n_pitch_shifts = np.array([-1, 1])
        idx_exist = np.isin(bins_to_augment, superres_label_to_bin_id[superres_label_to_orig_label==temp_orig])
        n_pitch_shifts = n_pitch_shifts[idx_exist]
        bins_to_augment = bins_to_augment[idx_exist] 
        if len(bins_to_augment):
            cmp = n_spikes_per_bin[k]
            superres_templates[k] = superres_templates[k]*n_spikes_per_bin[k]
            for j, bin_id_to_augment in enumerate(bins_to_augment):
                shift = n_pitch_shifts[j]
                idx_augment = np.flatnonzero(np.logical_and(superres_label_to_orig_label==temp_orig, superres_label_to_bin_id==bin_id_to_augment))
                if shift>0:
                    # shift by 1 row up
                    # set other channels ot 0
                    superres_templates[k] += pitch_shift_templates(
                                                shift, geom, superres_templates[idx_augment], fill_value=fill_value
                                            )[0]*n_spikes_per_bin[idx_augment]
                    superres_templates[k, :, :shift*n_chans_per_row]=0
                elif shift<0:
                    # shift by shift row down
                    # set other channels ot 0
                    superres_templates[k] += pitch_shift_templates(
                                                shift, geom, superres_templates[idx_augment], fill_value=fill_value
                                            )[0]*n_spikes_per_bin[idx_augment]
                    superres_templates[k, :, shift*n_chans_per_row:]=0
                cmp += n_spikes_per_bin[idx_augment]
            n_spikes_per_bin[k]=cmp
            superres_templates[k] /= cmp
         
    return superres_templates, n_spikes_per_bin
    


# %%
def shift_superres_templates(
    superres_templates,
    superres_label_to_bin_id,
    superres_label_to_orig_label,
    bin_size_um,
    geom,
    disp_value,
    registered_medians,
    medians_at_computation,
    fill_value=0.0,
):

    """
    This version shifts by every (possible - if enough templates) mod 
    """
    pitch = get_pitch(geom)
    bins_per_pitch = pitch / bin_size_um
    
    if bins_per_pitch != int(bins_per_pitch):
        raise ValueError(
            f"The pitch of this probe is {pitch}, but the bin size "
            f"{bin_size_um} does not evenly divide it."
        )
    
    shifted_templates = superres_templates.copy()

    #shift every unit separately
    for unit in np.unique(superres_label_to_orig_label):
        shift_um = disp_value + registered_medians[unit] - medians_at_computation[unit]
        # shift in bins, rounded towards 0
        bins_shift = np.round(shift_um / bin_size_um) # ROUND ??? - do mod, a little different
        if bins_shift!=0:
            # How to do the shifting?
            # We break the shift into two pieces: the number of full pitches,
            # and the remaining bins after shifting by full pitches.
            n_pitches_shift = int(
                bins_shift / bins_per_pitch
            )  # want to round towards 0, not //

            bins_shift_rem = bins_shift - bins_per_pitch * n_pitches_shift

            # Now, first we do the pitch shifts
            shifted_templates_unit = pitch_shift_templates(
                n_pitches_shift, geom, superres_templates[superres_label_to_orig_label==unit], fill_value=fill_value
            )
            # Now, do the mod shift bins_shift_rem
            # IDEA: take the bottom bin and shift it above - 
            # If more than pitch/2 templates - OK, can shift 
            # Only special case np.abs(bins_shift_rem)<=pitch/2 and n_temp <=pitch/2 -> better not to shift (no information gain)

            n_temp = (superres_label_to_orig_label==unit).sum()
            if bins_shift_rem<0:
                if bins_shift_rem<-pitch/2 or n_temp>pitch/2:
                    idx_mod_shift = np.flatnonzero(np.isin(superres_label_to_bin_id[superres_label_to_orig_label==unit], superres_label_to_bin_id[superres_label_to_orig_label==unit].min()-np.arange(-bins_shift_rem)+bins_per_pitch-1))
                    n_temp_shift = len(idx_mod_shift)
                    if n_temp_shift:
                        shifted_templates_unit[-n_temp_shift:] = pitch_shift_templates(
                            -1, geom, shifted_templates_unit[idx_mod_shift], fill_value=fill_value
                        ) 

                        # The rest of the shift is handled by updating bin ids
                        # This part doesn't matter for the recovered spike train, since
                        # the template doesn't change, but it could matter for z tracking
                        superres_label_to_bin_id[superres_label_to_orig_label==unit] = np.roll(superres_label_to_bin_id[superres_label_to_orig_label==unit], -len(idx_mod_shift))
            elif bins_shift_rem>0:
                if bins_shift_rem>pitch/2 or n_temp>pitch/2:
                    idx_mod_shift = np.flatnonzero(np.isin(superres_label_to_bin_id[superres_label_to_orig_label==unit], superres_label_to_bin_id[superres_label_to_orig_label==unit].max()+np.arange(bins_shift_rem)-bins_per_pitch+1))
                    n_temp_shift = len(idx_mod_shift)
                    if n_temp_shift:
                        shifted_templates_unit[:n_temp_shift] = pitch_shift_templates(
                            1, geom, shifted_templates_unit[idx_mod_shift], fill_value=fill_value
                        ) #shift by 1 pitch as we taked templates that are at max()+shift-pitch
                        # update bottom templates - we remove <= "space" at the bottom than we add on top

                        # The rest of the shift is handled by updating bin ids
                        # This part doesn't matter for the recovered spike train, since
                        # the template doesn't change, but it could matter for z tracking

                        # !!! That's an approximation - maybe we'll need to change if we do z tracking, shoul;d be fine for now - is ok if we have bins that are "continuous" per unit
                        superres_label_to_bin_id[superres_label_to_orig_label==unit] = np.roll(superres_label_to_bin_id[superres_label_to_orig_label==unit], n_temp_shift)
            shifted_templates[superres_label_to_orig_label==unit]=shifted_templates_unit

    return shifted_templates #, superres_label_to_bin_id


# %%
def shift_deconv(
    raw_bin,
    geom,
    p, #displacement
    bin_size_um,
    registered_medians,
    superres_templates,
    medians_at_computation,
    superres_label_to_bin_id,
    superres_label_to_orig_label,
    deconv_dir=None,
    pfs=30_000,
    t_start=0,
    t_end=None,
    n_jobs=1,
    trough_offset=42,
    spike_length_samples=121,
    max_upsample=1, #param for UHD - can increase if speed ok 
    refractory_period_frames=10,
    deconv_threshold=500, #Important param to validate
    su_chan_vis=3, #Important param to validate
):
    #multiprocessing needs to be fixed 
    n_processors = n_jobs
    if n_jobs ==0:
        n_processors=1
    # discover the pitch of the probe
    # this is the unit at which the probe repeats itself.
    # so for NP1, it's not every row, but every 2 rows!
    pitch = get_pitch(geom)
    print(f"a {pitch=}")

    # integer probe-pitch shifts at each time bin
    p = p[t_start : t_end if t_end is not None else len(p)]
    bin_shifts = (p + bin_size_um / 2) // bin_size_um * bin_size_um
    unique_shifts, shift_ids_by_time = np.unique(
        bin_shifts, return_inverse=True
    )

    # for each shift, get shifted templates
    shifted_templates = np.array(
        [
            shift_superres_templates(
                superres_templates,
                superres_label_to_bin_id,
                superres_label_to_orig_label,
                bin_size_um,
                geom,
                shift,
                registered_medians,
                medians_at_computation)
            for shift in unique_shifts
        ]
    )

    # run deconv on just the appropriate batches for each shift
    deconv_dir = Path(
        deconv_dir if deconv_dir is not None else tempfile.mkdtemp()
    )
    if n_jobs > 1:
        ctx = multiprocessing.get_context("spawn")
    shifted_templates_up = []
    shifted_sparse_temp_map = []
    sparse_temp_to_orig_map = []
    batch2shiftix = {}
    for shiftix, (shift, temps) in enumerate(
        zip(unique_shifts, tqdm(shifted_templates, desc="Shifts"))
    ):
        mp_object = deconvolve.MatchPursuitObjectiveUpsample(
            templates=temps,
            deconv_dir=deconv_dir,
            standardized_bin=raw_bin,
            t_start=t_start,
            t_end=t_end,
            n_sec_chunk=1,
            sampling_rate=pfs,
            max_iter=1000,
            threshold=deconv_threshold,
            vis_su=su_chan_vis,
            conv_approx_rank=5,
            n_processors=n_processors,
            multi_processing=n_jobs > 1,
            upsample=max_upsample,
            lambd=0,
            allowed_scale=0,
            template_index_to_unit_id=superres_label_to_orig_label,
            refractory_period_frames=refractory_period_frames,
        )
        my_batches = np.flatnonzero(bin_shifts == shift)
        for bid in my_batches:
            batch2shiftix[bid] = shiftix
        my_fnames = [
            deconv_dir / f"seg_{bid:06d}_deconv.npz" for bid in my_batches
        ]
        if n_jobs <= 1:
            mp_object.run(my_batches, my_fnames)
        else:
            with ctx.Pool(
                n_jobs,
                initializer=mp_object.load_saved_state,
            ) as pool:
                for res in tqdm(
                    pool.imap_unordered(
                        mp_object._run_batch,
                        zip(my_batches, my_fnames),
                    ),
                    total=len(my_batches),
                    desc="Template matching",
                ):
                    pass

        (
            templates_up,
            deconv_id_sparse_temp_map,
            sparse_id_to_orig_id,
        ) = mp_object.get_sparse_upsampled_templates(return_orig_map=True)
        templates_up = templates_up.transpose(2, 0, 1)
        shifted_templates_up.append(templates_up)
        shifted_sparse_temp_map.append(deconv_id_sparse_temp_map)
        sparse_temp_to_orig_map.append(sparse_id_to_orig_id)

        assert len(templates_up) == len(sparse_id_to_orig_id)

    # collect all shifted and upsampled templates
    shifted_upsampled_start_ixs = np.array(
        [0] + list(np.cumsum([t.shape[0] for t in shifted_templates_up[:-1]]))
    )
    all_shifted_upsampled_temps = np.concatenate(shifted_templates_up, axis=0)
    shifted_upsampled_idx_to_shift_id = np.concatenate(
        [[i] * t.shape[0] for i, t in enumerate(shifted_templates_up)], axis=0
    )
    shifted_upsampled_idx_to_orig_id = (
        np.concatenate(sparse_temp_to_orig_map, axis=0)
        # + shifted_upsampled_start_ixs[shifted_upsampled_idx_to_shift_id]
    )
    assert shifted_upsampled_idx_to_shift_id.shape == shifted_upsampled_idx_to_orig_id.shape

    # gather deconv resultsdeconv_st = []
    deconv_spike_train_shifted_upsampled = []
    deconv_spike_train = []
    deconv_scalings = []
    deconv_dist_metrics = []
    print("gathering deconvolution results")
    for bid in range(mp_object.n_batches):
        which_shiftix = batch2shiftix[bid]

        fname_out = deconv_dir / f"seg_{bid:06d}_deconv.npz"
        with np.load(fname_out) as d:
            st = d["spike_train"]
            deconv_scalings.append(d["scalings"])
            deconv_dist_metrics.append(d["dist_metric"])

        st[:, 0] += trough_offset

        # usual spike train
        deconv_st = st.copy()
        deconv_st[:, 1] //= max_upsample
        deconv_spike_train.append(deconv_st)

        # upsampled + shifted spike train
        st_up = st.copy()
        st_up[:, 1] = shifted_sparse_temp_map[which_shiftix][st_up[:, 1]]
        st_up[:, 1] += shifted_upsampled_start_ixs[which_shiftix]
        deconv_spike_train_shifted_upsampled.append(st_up)

        shift_good = (
            shifted_upsampled_idx_to_shift_id[st_up[:, 1]] == which_shiftix
        ).all()
        tsorted = (np.diff(st_up[:, 0]) >= 0).all()
        bigger = (bid == 0) or (
            st_up[:, 0] >= deconv_spike_train_shifted_upsampled[-2][:, 0].max()
        ).all()
        pitchy = (
            bin_shifts[((st_up[:, 0] - trough_offset) // pfs - t_start).astype(int)] == unique_shifts[which_shiftix]
        ).all()
        # print(f"{bid=} {shift_good=} {tsorted=} {bigger=} {pitchy=}")
        assert shift_good
        assert tsorted
        assert bigger
        if not pitchy:
            raise ValueError(
                f"{bid=} Not pitchy {np.unique(bin_shifts[((st_up[:, 0] - trough_offset) // pfs - t_start).astype(int)])=} "
                f"{which_shiftix=} {unique_shifts[which_shiftix]=} {np.unique((st_up[:, 0] - trough_offset) // pfs - t_start)=} "
                f"{bin_shifts[np.unique((st_up[:, 0] - trough_offset) // pfs - t_start)]=}"
            )

    deconv_spike_train = np.concatenate(deconv_spike_train, axis=0)
    deconv_spike_train_shifted_upsampled = np.concatenate(
        deconv_spike_train_shifted_upsampled, axis=0
    )
    deconv_scalings = np.concatenate(deconv_scalings, axis=0)
    deconv_dist_metrics = np.concatenate(deconv_dist_metrics, axis=0)

    print(
        f"Number of Spikes deconvolved: {deconv_spike_train_shifted_upsampled.shape[0]}"
    )

    return dict(
        deconv_spike_train=deconv_spike_train,
        deconv_spike_train_shifted_upsampled=deconv_spike_train_shifted_upsampled,
        deconv_scalings=deconv_scalings,
        shifted_templates=shifted_templates,
        all_shifted_upsampled_temps=all_shifted_upsampled_temps,
        shifted_upsampled_idx_to_orig_id=shifted_upsampled_idx_to_orig_id,
        shifted_upsampled_idx_to_shift_id=shifted_upsampled_idx_to_shift_id,
        deconv_dist_metrics=deconv_dist_metrics,
    )

# %%
def superres_deconv_chunk(
    raw_bin,
    geom,
    z_abs,
    x,
    p,
    spike_train,
    deconv_dir,
    registered_medians=None, #registered_median
    units_spread=None, #registered_spread
    dist_metric=None,
    bin_size_um=1,
    pfs=30_000,
    t_start=0,
    t_end=None,
    n_jobs=1,
    trough_offset=42,
    spike_length_samples=121,
    max_upsample=1,
    refractory_period_frames=10,
    min_spikes_bin=None,
    augment_low_snr_temps=True, 
    min_spikes_to_augment=25,
    max_spikes_per_unit=200,
    tpca=None,
    deconv_threshold=500,
    deconv_outliers_threshold=1000,
    su_chan_vis=1.5, 
    adaptive_th_for_temp_computation=False,
    outliers_tracking=None,
):

    Path(deconv_dir).mkdir(exist_ok=True)

    (
        superres_templates,
        superres_label_to_bin_id,
        superres_label_to_orig_label,
        medians_at_computation,
    ) = superres_denoised_templates(
        spike_train,
        z_abs,
        x,
        bin_size_um,
        geom,
        raw_bin,
        t_end,
        min_spikes_bin,
        augment_low_snr_temps, 
        min_spikes_to_augment,
        units_spread,
        dist_metric,
        deconv_outliers_threshold,
        adaptive_th_for_temp_computation,
        outliers_tracking,
        max_spikes_per_unit,
        n_spikes_max_recent=1000,
        denoise_templates=True,
        do_temporal_decrease=True,
        zero_radius_um=70,
        reducer=np.mean,
        snr_threshold=5.0 * np.sqrt(100),
        spike_length_samples=spike_length_samples,
        trough_offset=trough_offset,
        do_tpca=True,
        tpca=tpca,
        tpca_rank=5,
        tpca_radius=75,
        tpca_n_wfs=50_000,
        fs=pfs,
        seed=0,
        n_jobs=n_jobs,
    )

    shifted_deconv_res = shift_deconv(
        raw_bin,
        geom,
        p,
        bin_size_um,
        registered_medians,
        superres_templates,
        medians_at_computation,
        superres_label_to_bin_id,
        superres_label_to_orig_label,
        deconv_dir=deconv_dir,
        pfs=pfs,
        t_start=t_start,
        t_end=t_end,
        n_jobs=n_jobs,
        trough_offset=trough_offset,
        spike_length_samples=spike_length_samples,
        max_upsample=max_upsample,
        refractory_period_frames=refractory_period_frames,
        deconv_threshold=deconv_threshold,
        su_chan_vis=su_chan_vis,
    )

    # unpack results
    deconv_dist_metrics = shifted_deconv_res["deconv_dist_metrics"]
    superres_deconv_spike_train = shifted_deconv_res["deconv_spike_train"]
    superres_deconv_spike_train_shifted_upsampled = shifted_deconv_res[
        "deconv_spike_train_shifted_upsampled"
    ]
    deconv_scalings = shifted_deconv_res["deconv_scalings"]
    all_shifted_upsampled_temps = shifted_deconv_res[
        "all_shifted_upsampled_temps"
    ]
    shifted_upsampled_idx_to_superres_id = shifted_deconv_res[
        "shifted_upsampled_idx_to_orig_id"
    ]
    shifted_upsampled_idx_to_shift_id = shifted_deconv_res[
        "shifted_upsampled_idx_to_shift_id"
    ]

    # back to original label space
    deconv_spike_train = superres_deconv_spike_train.copy()
    deconv_spike_train[:, 1] = superres_label_to_orig_label[
        deconv_spike_train[:, 1]
    ]
    shifted_upsampled_idx_to_orig_id = superres_label_to_orig_label[
        shifted_upsampled_idx_to_superres_id
    ]
    shifted_upsampled_idx_to_superres_bin_id = superres_label_to_bin_id[
        shifted_upsampled_idx_to_superres_id
    ]

    # return everything the user could need
    return dict(
        deconv_spike_train=deconv_spike_train,
        superres_deconv_spike_train=superres_deconv_spike_train,
        superres_deconv_spike_train_shifted_upsampled=superres_deconv_spike_train_shifted_upsampled,
        deconv_scalings=deconv_scalings,
        superres_templates=superres_templates,
        superres_label_to_orig_label=superres_label_to_orig_label,
        superres_label_to_bin_id=superres_label_to_bin_id,
        all_shifted_upsampled_temps=all_shifted_upsampled_temps,
        shifted_upsampled_idx_to_superres_id=shifted_upsampled_idx_to_superres_id,
        shifted_upsampled_idx_to_superres_bin_id=shifted_upsampled_idx_to_superres_bin_id,
        shifted_upsampled_idx_to_orig_id=shifted_upsampled_idx_to_orig_id,
        shifted_upsampled_idx_to_shift_id=shifted_upsampled_idx_to_shift_id,
        trough_offset=trough_offset,
        spike_length_samples=spike_length_samples,
        bin_size_um=bin_size_um,
        raw_bin=raw_bin,
        deconv_dir=deconv_dir,
        deconv_dist_metrics=deconv_dist_metrics,
        shifted_superres_templates=shifted_deconv_res["shifted_templates"],
    )

# %%


# %%
def extract_superres_shifted_deconv(
    superres_deconv_result,
    overwrite=True,
    pbar=True,
    nn_denoise=True,
    output_directory=None,
    extract_radius_um=100,
    n_sec_train_feats=10, #HAVE TPCA READY BEFORE / subtraction h5
    # what to save / do?
    localize=True,
    loc_radius=100,
    # usual suspects
    sampling_rate=30000,
    n_sec_chunk=1,
    device=None,
    geom=None,
    subtraction_h5=None,
    n_jobs=-1,
):
    """
    This is a wrapper that helps us deal with the bookkeeping for proposed
    pairs and reassignment with the shifting and the superres and the
    upsampling and all that...
    """

    # infer what upsampled shifted superres units can be pairs
    shifted_upsampled_idx_to_shift_id = superres_deconv_result[
        "shifted_upsampled_idx_to_shift_id"
    ]
    shifted_upsampled_idx_to_superres_id = superres_deconv_result[
        "shifted_upsampled_idx_to_superres_id"
    ]
    shifted_upsampled_idx_to_orig_id = superres_deconv_result[
        "shifted_upsampled_idx_to_orig_id"
    ]
    print(f"{shifted_upsampled_idx_to_superres_id.shape=}")
    # print(",".join(map(str, shifted_upsampled_idx_to_superres_id)))

    if output_directory is None:
        output_directory = superres_deconv_result["deconv_dir"]

    ret = extract_deconv(
        superres_deconv_result["all_shifted_upsampled_temps"],
        superres_deconv_result[
            "superres_deconv_spike_train_shifted_upsampled"
        ],
        output_directory,
        superres_deconv_result["raw_bin"],
        scalings=superres_deconv_result["deconv_scalings"],
        geom=geom,
        extract_radius_um=extract_radius_um,
        subtraction_h5=subtraction_h5,
        save_residual=False,
        save_cleaned_waveforms=False,
        save_denoised_waveforms=False,
        save_cleaned_tpca_projs=False,
        save_denoised_tpca_projs=False,
        tpca_rank=8,
        tpca_weighted=False,
        save_outlier_scores=False,
        do_reassignment=False,
        save_reassignment_residuals=False,
        do_reassignment_tpca=False,
        reassignment_proposed_pairs_up=False,
        reassignment_tpca_rank=False,
        reassignment_norm_p=False,
        reassignment_tpca_spatial_radius=False,
        reassignment_tpca_n_wfs=False,
        localize=localize,
        loc_radius=loc_radius,
        n_sec_train_feats=n_sec_train_feats,
        n_jobs=n_jobs,
        n_sec_chunk=n_sec_chunk,
        sampling_rate=sampling_rate,
        device=device,
        trough_offset=superres_deconv_result["trough_offset"],
        overwrite=overwrite,
        pbar=pbar,
        nn_denoise=nn_denoise,
        seed=0,
    )

    extract_h5 = ret

    with h5py.File(extract_h5, "r+") as h5:
        # map the reassigned spike train from "shifted superres" label space
        # to both superres and the original label space, and store for user

        # store everything also for the user
        for key in (
            "deconv_spike_train",
            "superres_deconv_spike_train",
            "superres_deconv_spike_train_shifted_upsampled",
            "superres_templates",
            "superres_label_to_orig_label",
            "superres_label_to_bin_id",
            "all_shifted_upsampled_temps",
            "shifted_upsampled_idx_to_superres_id",
            "shifted_upsampled_idx_to_orig_id",
            "shifted_upsampled_idx_to_shift_id",
            "bin_size_um",
            "deconv_dist_metrics",
        ):
            h5.create_dataset(key, data=superres_deconv_result[key])

    return extract_h5

# %%


# %%
def full_deconv_with_update(
    deconv_dir,
    extract_dir,
    raw_bin,
    geom,
    p,
    spike_train,
    maxptps,
    x,
    z,
    T_START=0, 
    T_END=None,
    subtraction_h5=None,
    n_sec_temp_update=None, #length of chunks for template update 
    bin_size_um=1,
    pfs=30_000,
    n_jobs=1,
    trough_offset=42,
    spike_length_samples=121,
    max_upsample=1,
    refractory_period_frames=10,
    min_spikes_bin=None,
    augment_low_snr_temps=True, 
    min_spikes_to_augment=25,
    max_spikes_per_unit=200,
    tpca=None,
    deconv_threshold=500, #Validated experimentaly with template norm
    su_chan_vis=3, #Don't keep it too low so that templates effectively disappear when too far from the probe 
    deconv_th_for_temp_computation=1000, #Use only best spikes (or detected spikes) for temp computation
    adaptive_th_for_temp_computation=True,
    poly_params=[500, 200, 20, 1],
    extract_radius_um=100,
    loc_radius=100,
    n_sec_train_feats=10,
    n_sec_chunk=1,
    overwrite=True,
    p_bar=True,
    save_chunk_results=False,
):

    Path(extract_dir).mkdir(exist_ok=True)

    registered_medians, units_spread = get_registered_pos(spike_train, z, p, pfs)
    
    fname_medians = Path(extract_dir) / "registered_medians.npy"
    fname_spread = Path(extract_dir) / "registered_spreads.npy"
    np.save(fname_spread, units_spread)
    np.save(fname_medians, registered_medians)

    if adaptive_th_for_temp_computation:
        outliers_tracking = np.ones(len(spike_train), dtype=bool)
        dist_metric = deconv_th_for_temp_computation*2*np.ones(len(spike_train))

    elif deconv_th_for_temp_computation is not None:
        outliers_tracking = None
        dist_metric = deconv_th_for_temp_computation*2*np.ones(len(spike_train))

    for start_sec in tqdm(np.arange(T_START, T_END, n_sec_temp_update)):
        end_sec = min(start_sec+n_sec_temp_update, T_END)

        deconv_chunk_res = superres_deconv_chunk(
            raw_bin,
            geom,
            z,
            x,
            p,
            spike_train,
            deconv_dir,
            registered_medians=registered_medians, #registered_median
            units_spread=units_spread, #registered_spread
            dist_metric=dist_metric,
            bin_size_um=bin_size_um,
            pfs=pfs,
            t_start=start_sec,
            t_end=end_sec,
            n_jobs=n_jobs,
            trough_offset=trough_offset,
            spike_length_samples=spike_length_samples,
            max_upsample=max_upsample,
            refractory_period_frames=refractory_period_frames,
            min_spikes_bin=min_spikes_bin,
            augment_low_snr_temps=augment_low_snr_temps, 
            min_spikes_to_augment=min_spikes_to_augment,
            max_spikes_per_unit=max_spikes_per_unit,
            tpca=tpca,
            deconv_threshold=deconv_threshold, 
            deconv_outliers_threshold=deconv_th_for_temp_computation,
            su_chan_vis=su_chan_vis,
            adaptive_th_for_temp_computation=adaptive_th_for_temp_computation,
            outliers_tracking=outliers_tracking,
        )

        extract_deconv_chunk = extract_superres_shifted_deconv(
            deconv_chunk_res,
            overwrite=overwrite,
            pbar=p_bar,
            nn_denoise=True,
            output_directory=extract_dir,
            extract_radius_um=extract_radius_um,
            n_sec_train_feats=10, #HAVE TPCA READY BEFORE / subtraction h5
            localize=True,
            loc_radius=loc_radius,
            # usual suspects
            sampling_rate=pfs,
            n_sec_chunk=n_sec_chunk,
            device=None,
            geom=geom,
            subtraction_h5=subtraction_h5,
            n_jobs=n_jobs,
        )

        with h5py.File(extract_deconv_chunk, "r+") as h5:
            spt_chunk = h5["deconv_spike_train"][:]
            maxptps_chunk = h5["maxptps"][:]
            localizations_chunk = h5["localizations"][:]
            dist_metric_chunk = h5["deconv_dist_metrics"][:]
            if adaptive_th_for_temp_computation:
                superres_templates_chunk = h5["superres_templates"][:]
                superres_deconv_spike_train_chunk = h5["superres_deconv_spike_train"][:]
                superres_label = superres_deconv_spike_train_chunk[:, 1]
                ptps_temp_spikes = superres_templates_chunk.ptp(1).max(1)[superres_label]
                outliers_tracking_chunk = dist_metric_chunk > (p[0] + p[1]*ptps_temp_spikes + p[2]*ptps_temp_spikes**2 + p[3]*ptps_temp_spikes**3) 
            else:
                outliers_tracking_chunk=None
                
        if save_chunk_results:
            fname_ptps = Path(extract_dir) / "maxptps_deconv_{}_{}".format(start_sec, end_sec)
            fname_spike_train = Path(extract_dir) / "spike_train_deconv_{}_{}".format(start_sec, end_sec)
            fname_localizations = Path(extract_dir) / "localizations_deconv_{}_{}".format(start_sec, end_sec)
            fname_dist_metric = Path(extract_dir) / "dist_metric_deconv_{}_{}".format(start_sec, end_sec)

            np.save(fname_ptps, maxptps_chunk)
            np.save(fname_spike_train, spt_chunk)
            np.save(fname_localizations, localizations_chunk)
            np.save(fname_dist_metric, dist_metric_chunk)
            
            if adaptive_th_for_temp_computation:
                fname_ptps_spikes_temps = Path(extract_dir) / "ptps_temp_before_deconv_{}_{}".format(start_sec, end_sec)
                np.save(fname_ptps_spikes_temps, ptps_temp_spikes)

        
        spike_train, x, z, dist_metric, maxptps, outliers_tracking = update_spike_train_with_deconv_res(start_sec, end_sec, 
                                                            spike_train, spt_chunk,
                                                            x, z, localizations_chunk,
                                                            dist_metric, dist_metric_chunk,
                                                            maxptps, maxptps_chunk, 
                                                            outliers_tracking, outliers_tracking_chunk,
                                                            pfs, adaptive_th_for_temp_computation)
    
    # SAVE FULL RESULT 
    fname_ptps = Path(extract_dir) / "maxptps_final_deconv"
    fname_spike_train = Path(extract_dir) / "spike_train_final_deconv"
    fname_x = Path(extract_dir) / "x_final_deconv"
    fname_z = Path(extract_dir) / "z_final_deconv"
    fname_dist_metric = Path(extract_dir) / "dist_metric_final_deconv"

    np.save(fname_ptps, maxptps)
    np.save(fname_spike_train, spike_train)
    np.save(fname_x, x)
    np.save(fname_z, z)
    np.save(fname_dist_metric, dist_metric)
    
    if adaptive_th_for_temp_computation:
        fname_outlier_tracking = Path(extract_dir) / "outliers_final_deconv"
        np.save(fname_outlier_tracking, outliers_tracking)


# %%

# %%

# %%
def update_spike_train_with_deconv_res(start_sec, end_sec, spt_before, spt_after,
                                      x_before, z_before, localizations_after, dist_metric_before, dist_metric_after, 
                                      maxptps_before, maxptps_after, outliers_tracking, outliers_tracking_chunk,
                                      pfs=30000, adaptive_th_for_temp_computation=False):

    """
    Keep clustering results if no spikes deconvolved in start_sec end sec
    """

    x_after = localizations_after[:, 0]
    z_after = localizations_after[:, 2]
    
    idx_units_to_add = np.flatnonzero(np.logical_and(spt_before[:, 0]>=start_sec*pfs, spt_before[:, 0]<end_sec*pfs))
    units_to_add = np.setdiff1d(np.unique(spt_before[idx_units_to_add, 1]), np.unique(spt_after[:, 1]))
    
    idx_before = np.flatnonzero(np.logical_or(spt_before[:, 0]<start_sec*pfs, spt_before[:, 0]>=end_sec*pfs))
    spt_after = np.concatenate((spt_before[idx_before], spt_after))
    x_after = np.concatenate((x_before[idx_before], x_after))
    z_after = np.concatenate((z_before[idx_before], z_after))
    dist_metric_after = np.concatenate((dist_metric_before[idx_before], dist_metric_after))
    maxptps_after = np.concatenate((maxptps_before[idx_before], maxptps_after))
    if adaptive_th_for_temp_computation:
        outliers_tracking_chunk = np.concatenate((outliers_tracking[idx_before], outliers_tracking_chunk))
    
    for unit in units_to_add:
        idx_unit = idx_units_to_add[spt_before[idx_units_to_add, 1]==unit]
        spt_after = np.concatenate((spt_before[idx_unit], spt_after))
        x_after = np.concatenate((x_before[idx_unit], x_after))
        z_after = np.concatenate((z_before[idx_unit], z_after))
        dist_metric_after = np.concatenate((dist_metric_before[idx_unit], dist_metric_after))
        maxptps_after = np.concatenate((maxptps_before[idx_unit], maxptps_after))
        if adaptive_th_for_temp_computation:
            outliers_tracking_chunk = np.concatenate((outliers_tracking[idx_unit], outliers_tracking_chunk))
    
    idx_sort_by_time = spt_after[:, 0].argsort()
    x_after = x_after[idx_sort_by_time]
    z_after = z_after[idx_sort_by_time]
    dist_metric_after = dist_metric_after[idx_sort_by_time]
    maxptps_after = maxptps_after[idx_sort_by_time]
    spt_after = spt_after[idx_sort_by_time]
    if adaptive_th_for_temp_computation:
        outliers_tracking_chunk = outliers_tracking_chunk[idx_sort_by_time]

    return spt_after.astype('int'), x_after, z_after, dist_metric_after, maxptps_after, outliers_tracking_chunk


# %%

# %%
def get_registered_pos(spt, z, displacement_rigid, pfs=30000):
    z_reg = z-displacement_rigid[spt[:, 0]//pfs]
    registered_median = np.zeros(spt[:, 1].max()+1)
    registered_spread = np.zeros(spt[:, 1].max()+1)
    for k in np.unique(spt[:, 1]):
        registered_median[k] = np.median(z_reg[spt[:, 1]==k])
        registered_spread[k] = np.std(z_reg[spt[:, 1]==k])*1.65

    return registered_median, registered_spread

# %%
