# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Training script for MixNerf."""
import os
import functools
import gc
import time

from absl import app
import flax
from flax.metrics import tensorboard
from flax.training import checkpoints
from internal import configs, datasets, math, models, utils, vis  # pylint: disable=g-multiple-import
import jax
from jax import random
import jax.numpy as jnp
import numpy as np
from skimage.metrics import structural_similarity
import internal.helper as helper
from loss import S3IM

from tensorflow.compat.v1 import ConfigProto
from tensorflow.compat.v1 import InteractiveSession

configs.define_common_flags()
jax.config.parse_flags_with_absl()

TIME_PRECISION = 1000  # Internally represent integer times in milliseconds.


@flax.struct.dataclass
class TrainStats:
  """Collection of stats for logging."""
  loss: float
  losses: float
  disp_mses: float
  normal_maes: float
  weight_l2: float
  psnr: float
  psnrs: float
  grad_norm: float
  grad_abs_max: float
  grad_norm_clipped: float


def tree_sum(tree):
  return jax.tree_util.tree_reduce(lambda x, y: x + y, tree, initializer=0)


def tree_norm(tree):
  return jnp.sqrt(tree_sum(jax.tree_map(lambda x: jnp.sum(x**2), tree)))


def train_step(
    model,
    config,
    rng,
    state,
    batch,
    learning_rate,
    resample_padding,
    nll_loss_weight,
    stage,
):
  """One optimization step.

  Args:
    model: The linen model.
    config: The configuration.
    rng: jnp.ndarray, random number generator.
    state: utils.TrainState, state of the model/optimizer.
    batch: dict, a mini-batch of data for training.
    learning_rate: float, real-time learning rate.
    resample_padding: float, the histogram padding to use when resampling.
    tvnorm_loss_weight: float, tvnorm loss weight.

  Returns:
    A tuple (new_state, stats, rng) with
      new_state: utils.TrainState, new training state.
      stats: list. [(loss, psnr), (loss_coarse, psnr_coarse)].
      rng: jnp.ndarray, updated random number generator.
  """
  rng, key, key2 = random.split(rng, 3)

  def compute_nll(mu, pi, gamma, gt, mask, dist='laplace'):
      eps = 1e-12
      gamma = gamma + eps

      if dist == 'laplace':
          pdf = -(jnp.abs(gt - mu) / gamma)
          pdf = jnp.exp(pdf) / (2 * gamma)
      elif dist == 'gaussian':
          pdf = (gt - mu) / gamma
          pdf = -0.5 * (pdf * pdf)
          pdf = jnp.exp(pdf) / (gamma * jnp.sqrt(2.0 * jnp.pi))
      elif dist == 'cauchy':
          pdf = ((gt - mu) / gamma) ** 2
          pdf = 1 / (jnp.pi * gamma * (pdf + 1))
      else:
          raise Exception("Not implemented mixture distribution")

      multi_lh = jnp.prod(pdf, axis=2)
      md_lh = jnp.sum(pi * multi_lh, axis=1)
      nll = -jnp.log(md_lh + eps) * mask[..., 0]
      
      return nll
  


  def loss_fn(variables):
    s3im_func = S3IM(kernel_size=config.s3im_kernel, stride=config.s3im_stride, 
                     repeat_time=config.s3im_repeat_time, 
                     patch_height=config.s3im_patch_height, patch_width=config.s3im_patch_width)
    weight_l2 = (
        tree_sum(jax.tree_map(lambda z: jnp.sum(z**2), variables)) / tree_sum(
            jax.tree_map(lambda z: jnp.prod(jnp.array(z.shape)), variables)))

    renderings = model.apply(
        variables,
        key if config.randomized else None,
        batch['rays'],
        resample_padding=resample_padding,
        compute_extras=(config.compute_disp_metrics or
                        config.compute_normal_metrics),
        mode = 'train',
        freq_reg_mask=batch['freq_reg_mask'] if config.freq_reg else None,
        )
    
    xxx, ray_history = renderings
    
    lossmult = batch['rays'].lossmult
    if config.disable_multiscale_loss:
      lossmult = jnp.ones_like(lossmult)
   
    losses = []
    disp_mses = []
    normal_maes = []
    all_depth_loss = []

    for i, rendering in enumerate(renderings):
      #1024 3
      # 1.mse
      # 3 2 1 0
      strr = str(stage)
      if strr == '3':
        cur = rendering['rgb'] - batch['rgb3'][Ellipsis, :3]
      elif strr == '2':
        cur = rendering['rgb'] - batch['rgb2'][Ellipsis, :3]
      elif strr == '1':
        cur = rendering['rgb'] - batch['rgb1'][Ellipsis, :3]
      else:
        cur = rendering['rgb'] - batch['rgb'][Ellipsis, :3]
      
      # cur = rendering['rgb'] - batch['rgb'][Ellipsis, :3] # xiaorong
      numer = (lossmult * cur **2).sum()
      denom = lossmult.sum()
      losses.append(numer / denom)

      #2.ltmse
      # eta = 1e-3
      # f=lambda x: 0.5 - jnp.sin(jax.lax.asin(1.0 - 2.0 * (x + eta)) / 3.0)
      # loss = (f(rendering['rgb']) - f(batch['rgb'][Ellipsis, :3]))**2
      # denom = lossmult.sum()
      # losses.append((lossmult * loss).sum() / denom)

      #3.raw
      # rgb_render_clip = jnp.minimum(1., rendering['rgb'])
      # resid_sq_clip = (rgb_render_clip - batch['rgb'][..., :3]) ** 2
      # # Scale by gradient of log tonemapping curve.
      # scaling_grad = 1. / (1e-3 + jax.lax.stop_gradient(rgb_render_clip))
      # # Reweighted L2 loss.
      # data_loss = resid_sq_clip * scaling_grad ** 2
      # denom = lossmult.sum()
      # losses.append((lossmult * data_loss).sum() / denom)

      depth = rendering['distance_mean']
      depth = depth.reshape(-1,4).transpose() # two pairs of points, so here is 4
      margin1 = 1e-4
      margin2 = 1e-4
      depth_loss0_0 = jnp.mean(jnp.maximum(depth[0,:]-depth[1,:]+margin1,0)) ###
      # depth_loss0_1 = jnp.mean(jnp.maximum(jnp.abs(depth[0,:]-depth[2,:])-margin2,0)) 
      # depth_loss0_2 = jnp.mean(jnp.maximum(jnp.abs(depth[1,:]-depth[3,:])-margin2,0)) 
      depth_loss0_1 = 0.0 
      depth_loss0_2 = 0.0

      if config.dataset_loader=="llff":
        depth_loss = depth_loss0_0+(depth_loss0_1+depth_loss0_2)*0.01
      else:
        depth_loss = depth_loss0_0+(depth_loss0_1+depth_loss0_2)*1.0

      all_depth_loss.append(depth_loss)
      # all_depth_loss.append(0.0)


      if i != 0:
        # 1 mse
        # numerr = (lossmult * (rendering['R'] - batch['rgb_he'][..., :3])**2).sum()
        # denom = lossmult.sum()
        # losses.append(numerr / denom)
        
        # 2 d
        # sinkhorn_loss = helper.wasserstein_loss(rendering['R'], batch['rgb_he'][..., :3])
        # losses.append(sinkhorn_loss)
        hist_loss = helper.hist_loss()(rendering['R'], batch['rgb_he'][..., :3])
        losses.append(hist_loss)





      if i == 0:
        if config.nll_loss:
          nll_loss = compute_nll(rendering['mu'], rendering['pi'], rendering['gamma'], batch['rgb'][:, None, :], lossmult, dist='laplace')
          losses.append(nll_loss.sum() / lossmult.sum())
        else:
          losses.append(0.0)

        if config.raydepth_nll_loss:
          raydepth_nll_loss = compute_nll(rendering['mu_depth'][Ellipsis, None], rendering['pi'], rendering['gamma'], rendering['dirs_norm'][Ellipsis, None], lossmult, dist='laplace')
          losses.append(raydepth_nll_loss.sum() / lossmult.sum())
        else:
          losses.append(0.0)
        
        if config.remodel_nll_loss:
          remodel_nll_loss = compute_nll(rendering['mu'], rendering['pi_regen'], rendering['gamma'], batch['rgb'][:, None, :], lossmult, dist='laplace')
          losses.append(remodel_nll_loss.sum() / lossmult.sum())
        else:
          losses.append(0.0)
      
      # loss_spa = helper.rubeStructure_Loss(contrast=0.4*10)(batch['rgb'], rendering['rgb']) #0.28/0.15
      # losses.append(loss_spa)
          
      # loss_light = helper.Exp_lossPost()(batch['rgb'][Ellipsis, :3], rendering['rgb'])
      # losses.append(loss_light)

      # loss_TV = helper.L1_TV(rendering['light'])
      # losses.append(loss_TV)

      # mean_rgb = jnp.mean(rendering['rgb'], axis=0)
      # loss_col = helper.colour(mean_rgb)
      # losses.append(loss_col)
      
      
      # loss_med = helper.Exp_loss(16)(rendering['c_med'], 0.95)
      # # loss_med = helper.medloss(rendering['sigma_med'], 1.2)
      # losses.append(loss_med)
      
      # loss_w = helper.weightsloss(rendering['weights'])
      # losses.append(loss_w)
      #
      loss_TV = helper.L1_TV(rendering['light'])
      losses.append(loss_TV)
      
      ########################
      mean_rgb = jnp.mean(rendering['R'], axis=0)
      loss_rgb = helper.colour(mean_rgb)
      losses.append(loss_rgb)
      
      # # losses.append(loss_TV)
      # loss_light = helper.Exp_loss(16)(1.0 - rendering['tar'], 0.6)
      # losses.append(loss_light)############



      if config.compute_disp_metrics:
        # Using mean to compute disparity, but other distance statistics can be
        # used instead.
        disp = 1 / (1 + rendering['distance_mean'])
        disp_mses.append(((disp - batch['disps'])**2).mean())
      
      if config.compute_normal_metrics:
        one_eps = 1 - jnp.finfo(jnp.float32).eps
        normal_mae = jnp.arccos(
            jnp.clip(
                jnp.sum(batch['normals'] * rendering['normals'], axis=-1),
                -one_eps, one_eps)).mean()
        normal_maes.append(normal_mae)

    render_random_rays = ((config.depth_tvnorm_loss_mult != 0.0) or
                          (config.depth_tvnorm_decay))
    if render_random_rays:
      losses_georeg = []
      renderings_random = model.apply(
          variables,
          key2 if config.randomized else None,
          batch['rays_random'],
          resample_padding=resample_padding,
          compute_extras=True,
          ## ---- add freq_reg_mask as input ---- ##
          freq_reg_mask=batch['freq_reg_mask'] if config.freq_reg else None,
          mode = 'train')
          ## ------------------------------------ ##)
      ps = config.patch_size
      reshape_to_patch = lambda x, dim: x.reshape(-1, ps, ps, dim)
      for i, rendering in enumerate(renderings_random):
        if config.depth_tvnorm_loss_mult != 0.0 or config.depth_tvnorm_decay:
          depth = reshape_to_patch(rendering[config.depth_tvnorm_selector], 1)
          weighting = jax.lax.stop_gradient(
                  reshape_to_patch(
                      rendering['acc'],
                         1)[:, :-1, :-1]) * config.depth_tvnorm_mask_weight
          losses_georeg.append(
                  math.compute_tv_norm(depth, config.depth_tvnorm_type,
                                        weighting).mean())
        else:
          losses_georeg.append(0.0)
    losses = jnp.array(losses)
    disp_mses = jnp.array(disp_mses)
    normal_maes = jnp.array(normal_maes)
    all_depth_loss = jnp.array(all_depth_loss)
    # 1 mse
    # loss = ( losses[-3] + 
    #         5 * 1e-1 * losses[-2] + 
    #         config.coarse_loss_mult * losses[0] +
    #         nll_loss_weight * losses[1] +
    #         config.raydepth_nll_weight * losses[2] +
    #         config.remodel_nll_weight * losses[3] +
    #         config.weight_decay_mult * weight_l2           
    #         + 1e-3 * (losses[4] + losses[-1]) #-7
    #         # + 1e-3 * (losses[6] + losses[-1])
          # )
    # 2 w dis
    loss = ( losses[-4] + 
            losses[-3] + 
            config.coarse_loss_mult * losses[0] +
            nll_loss_weight * losses[1] +
            config.raydepth_nll_weight * losses[2] +
            config.remodel_nll_weight * losses[3] +
            config.weight_decay_mult * weight_l2           
            + 1e-3 * (losses[4] + losses[-2]) #-7
            + 1e-3 * (losses[5] + losses[-1]) 
            + 0.5 * all_depth_loss[-1]
          )
    
            

    if config.occ_reg_loss_mult > 0.0:
      last_ray_results = ray_history
      rgb = last_ray_results['mu']
      density = last_ray_results['pi']
      occ_reg_loss = jnp.mean(math.lossfun_occ_reg(
        rgb, density, reg_range=config.occ_reg_range,
        # wb means white&black prior in DTU
        wb_prior=config.occ_wb_prior, wb_range=config.occ_wb_range)) 
      occ_reg_loss = config.occ_reg_loss_mult * occ_reg_loss
      loss += occ_reg_loss
    

    return loss, (losses, disp_mses, normal_maes, weight_l2)

  (loss, loss_aux), grad = (jax.value_and_grad(loss_fn, has_aux=True)(
      state.optimizer.target))
  (losses, disp_mses, normal_maes, weight_l2) = loss_aux
  grad = jax.lax.pmean(grad, axis_name='batch')
  losses = jax.lax.pmean(losses, axis_name='batch')
  disp_mses = jax.lax.pmean(disp_mses, axis_name='batch')
  normal_maes = jax.lax.pmean(normal_maes, axis_name='batch')
  weight_l2 = jax.lax.pmean(weight_l2, axis_name='batch')
  # all_depth_loss = jax.lax.pmean(all_depth_loss, axis_name='batch')

  if config.check_grad_for_nans:
    grad = jax.tree_map(jnp.nan_to_num, grad)

  if config.grad_max_val > 0:
    grad = jax.tree_map(
        lambda z: jnp.clip(z, -config.grad_max_val, config.grad_max_val), grad)

  grad_abs_max = jax.tree_util.tree_reduce(
      lambda x, y: jnp.maximum(x, jnp.max(jnp.abs(y))), grad, initializer=0)

  grad_norm = tree_norm(grad)
  if config.grad_max_norm > 0:
    mult = jnp.minimum(
        1, config.grad_max_norm / (jnp.finfo(jnp.float32).eps + grad_norm))
    grad = jax.tree_map(lambda z: mult * z, grad)
  grad_norm_clipped = tree_norm(grad)

  new_optimizer = state.optimizer.apply_gradient(
      grad, learning_rate=learning_rate)
  new_state = state.replace(optimizer=new_optimizer)

  psnrs = math.mse_to_psnr(losses)
  stats = TrainStats(
      loss=loss,
      losses=losses,
      disp_mses=disp_mses,
      normal_maes=normal_maes,
      weight_l2=weight_l2,
      psnr=psnrs[-1],
      psnrs=psnrs,
      grad_norm=grad_norm,
      grad_abs_max=grad_abs_max,
      grad_norm_clipped=grad_norm_clipped,
  )

  return new_state, stats, rng


def main(unused_argv):
  
  rng = random.PRNGKey(20200823)
  # Shift the numpy random seed by host_id() to shuffle data loaded by different
  # hosts.
  np.random.seed(20201473 + jax.host_id())

  config = configs.load_config()

  if config.batch_size % jax.device_count() != 0:
    raise ValueError('Batch size must be divisible by the number of devices.')

  dataset = datasets.load_dataset('train', config.data_dir, config)
  test_dataset = datasets.load_dataset('test', config.data_dir, config)

  rng, key = random.split(rng)
  model, variables = models.construct_mipnerf(
      key,
      dataset.peek()['rays'],
      config,
  )
  num_params = jax.tree_util.tree_reduce(
      lambda x, y: x + jnp.prod(jnp.array(y.shape)), variables, initializer=0)
  print(f'Number of parameters being optimized: {num_params}')

  optimizer = flax.optim.Adam(config.lr_init).create(variables)
  state = utils.TrainState(optimizer=optimizer)
  del optimizer, variables

  train_pstep = jax.pmap(
      functools.partial(train_step, model, config), axis_name='batch',
      in_axes=(0, 0, 0, None, None, None, None))

  # Because this is only used for test set rendering, we disable randomization
  # and use the "final" padding for resampling.
  
  def render_eval_fn(variables, _, rays):
    return jax.lax.all_gather(
        model.apply(
            variables,
            None,  # Deterministic.
            rays,
            resample_padding=config.resample_padding_final,
            compute_extras=True,
            mode = 'test',), axis_name='batch')

  render_eval_pfn = jax.pmap(
      render_eval_fn,
      axis_name='batch',
      in_axes=(None, None, 0),  # Only distribute the data input.
      donate_argnums=(3,),
  )

  def ssim_fn(x, y):
    return structural_similarity(x, y, multichannel=True, data_range=1.0, win_size=11, gaussian_weights=True, sigma=1.5, use_sample_covariance=False, K1=0.01, K2=0.03)

  if not utils.isdir(config.checkpoint_dir):
    utils.makedirs(config.checkpoint_dir)
  state = checkpoints.restore_checkpoint(config.checkpoint_dir, state)
  # Resume training at the step of the last checkpoint.
  init_step = state.optimizer.state.step + 1
  state = flax.jax_utils.replicate(state)

  if jax.host_id() == 0:
    summary_writer = tensorboard.SummaryWriter(config.checkpoint_dir)
    summary_writer.text('config', f'<pre>{config}</pre>', step=0)

  # Prefetch_buffer_size = 3 x batch_size
  pdataset = flax.jax_utils.prefetch_to_device(dataset, 3)
  rng = rng + jax.host_id()  # Make random seed separate across hosts.
  rngs = random.split(rng, jax.local_device_count())  # For pmapping RNG keys.
  gc.disable()  # Disable automatic garbage collection for efficiency.
  total_time = 0
  total_steps = 0
  avg_psnr_numer = 0.
  avg_psnr_denom = 0
  train_start_time = time.time()
  for step, batch in zip(range(init_step, config.max_steps + 1), pdataset):
     ## ---- a tricky way to use freq_reg_mask ------------- ##
    if config.freq_reg:
      batch['freq_reg_mask'] = (
        math.get_freq_reg_mask(99, step, config.freq_reg_end, config.max_vis_freq_ratio).tile((batch['rays'].origins.shape[0], 1)),
        math.get_freq_reg_mask(27, step, config.freq_reg_end, config.max_vis_freq_ratio).tile((batch['rays'].origins.shape[0], 1)))
    ## ------------------------------------------- ##

    learning_rate = math.learning_rate_decay(
        step,
        config.lr_init,
        config.lr_final,
        config.max_steps,
        config.lr_delay_steps,
        config.lr_delay_mult,
    )

    resample_padding = math.log_lerp(
        step / config.max_steps,
        config.resample_padding_init,
        config.resample_padding_final,
    )

    if config.nll_loss_decay:
      nll_loss_weight = math.compute_tvnorm_weight(
          step, config.nll_loss_maxstep,
          config.nll_loss_weight_start,
          config.nll_loss_weight_end)
    else:
      nll_loss_weight = config.nll_loss_mult
    # 0903
    factor_gau = 0.7
    if step <= int(config.max_steps / 4 * factor_gau):
      stage = 3
    elif int(config.max_steps / 4 * factor_gau) < step <= int(config.max_steps / 2 * factor_gau):
      stage = 2
    elif int(config.max_steps / 2 * factor_gau) < step <= int(config.max_steps / 4 * factor_gau * 3):
      stage = 1
    else:
      stage = 0
    # stage = 0

    state, stats, rngs = train_pstep(
        rngs,
        state,
        batch,
        learning_rate,
        resample_padding,
        nll_loss_weight,
        stage,
    )

    if step % config.gc_every == 0:
      gc.collect()  # Disable automatic garbage collection for efficiency.

    # Log training summaries. This is put behind a host_id check because in
    # multi-host evaluation, all hosts need to run inference even though we
    # only use host 0 to record results.
    if jax.host_id() == 0:
      avg_psnr_numer += stats.psnr[0]
      avg_psnr_denom += 1
      if step % config.print_every == 0:
        elapsed_time = time.time() - train_start_time
        steps_per_sec = config.print_every / elapsed_time
        rays_per_sec = config.batch_size * steps_per_sec

        # A robust approximation of total training time, in case of pre-emption.
        total_time += int(round(TIME_PRECISION * elapsed_time))
        total_steps += config.print_every
        approx_total_time = int(round(step * total_time / total_steps))

        avg_psnr = avg_psnr_numer / avg_psnr_denom
        avg_psnr_numer = 0.
        avg_psnr_denom = 0

        # For some reason, the `stats` object has a superfluous dimension.
        stats = jax.tree_map(lambda x: x[0], stats)
        summary_writer.scalar('num_params', num_params, step)
        summary_writer.scalar('train_loss', stats.loss, step)
        summary_writer.scalar('train_psnr', stats.psnr, step)
        if config.compute_disp_metrics:
          for i, disp_mse in enumerate(stats.disp_mses):
            summary_writer.scalar(f'train_disp_mse_{i}', disp_mse, step)
        if config.compute_normal_metrics:
          for i, normal_mae in enumerate(stats.normal_maes):
            summary_writer.scalar(f'train_normal_mae_{i}', normal_mae, step)
        summary_writer.scalar('train_avg_psnr', avg_psnr, step)
        summary_writer.scalar('train_avg_psnr_timed', avg_psnr,
                              total_time // TIME_PRECISION)
        summary_writer.scalar('train_avg_psnr_timed_approx', avg_psnr,
                              approx_total_time // TIME_PRECISION)
        for i, l in enumerate(stats.losses):
          summary_writer.scalar(f'train_losses_{i}', l, step)
        for i, p in enumerate(stats.psnrs):
          summary_writer.scalar(f'train_psnrs_{i}', p, step)
        summary_writer.scalar('weight_l2', stats.weight_l2, step)
        summary_writer.scalar('train_grad_norm', stats.grad_norm, step)
        summary_writer.scalar('train_grad_norm_clipped',
                              stats.grad_norm_clipped, step)
        summary_writer.scalar('train_grad_abs_max', stats.grad_abs_max, step)
        summary_writer.scalar('learning_rate', learning_rate, step)
        summary_writer.scalar('resample_padding', resample_padding, step)
        summary_writer.scalar('train_steps_per_sec', steps_per_sec, step)
        summary_writer.scalar('train_rays_per_sec', rays_per_sec, step)
        precision = int(np.ceil(np.log10(config.max_steps))) + 1
        print(f'{step:{precision}d}' + f'/{config.max_steps:d}: ' +
              f'loss={stats.loss:0.4f}, ' + f'avg_psnr={avg_psnr:0.2f}, ' +
              f'weight_l2={stats.weight_l2:0.2e}, ' +
              f'lr={learning_rate:0.2e}, '
              f'pad={resample_padding:0.2e}, ' +
              f'{rays_per_sec:0.0f} rays/sec')

        train_start_time = time.time()

      if step % config.checkpoint_every == 0:
        state_to_save = jax.device_get(jax.tree_map(lambda x: x[0], state))
        checkpoints.save_checkpoint(
            config.checkpoint_dir, state_to_save, int(step), keep=100)

    # Test-set evaluation.
    if config.train_render_every > 0 and step % config.train_render_every == 0:
      # We reuse the same random number generator from the optimization step
      # here on purpose so that the visualization matches what happened in
      # training.
      if config.freq_reg:
        ## -- re-defining the rendering eval function to use current freq_reg_mask -- ##
        freq_reg_mask = (
          math.get_freq_reg_mask(99, step, config.freq_reg_end, config.max_vis_freq_ratio),
          math.get_freq_reg_mask(27, step, config.freq_reg_end, config.max_vis_freq_ratio))
        def render_eval_fn(variables, _, rays):
            return jax.lax.all_gather(
              model.apply(
                  variables,
                  None,  # Deterministic.
                  rays,
                  resample_padding=config.resample_padding_final,
                  mode = 'test',
                  compute_extras=True, freq_reg_mask=freq_reg_mask,
                  ), axis_name='batch')

        render_eval_pfn = jax.pmap(
            render_eval_fn,
            axis_name='batch',
            in_axes=(None, None, 0),  # Only distribute the data input.
            donate_argnums=(3,))
      ## ---------------------------------------------------------------------- ##

      eval_start_time = time.time()
      eval_variables = jax.device_get(jax.tree_map(lambda x: x[0],
                                                   state)).optimizer.target
      test_case = next(test_dataset)
      rendering = models.render_image(
          functools.partial(render_eval_pfn, eval_variables),
          test_case['rays'],
          rngs[0],
          config)

      vis_start_time = time.time()
      vis_suite = vis.visualize_suite(rendering, test_case['rays'], config)
      print(f'Visualized in {(time.time() - vis_start_time):0.3f}s')

      # Log eval summaries on host 0.
      if jax.host_id() == 0:
        if not config.render_path:
          psnr = float(
              math.mse_to_psnr(((
                  rendering['rgb'] - test_case['rgb'])**2).mean()))
          ssim = float(ssim_fn(rendering['rgb'], test_case['rgb']))
        eval_time = time.time() - eval_start_time
        num_rays = jnp.prod(jnp.array(test_case['rays'].directions.shape[:-1]))
        rays_per_sec = num_rays / eval_time
        summary_writer.scalar('test_rays_per_sec', rays_per_sec, step)
        print(f'Eval {step}: {eval_time:0.3f}s., {rays_per_sec:0.0f} rays/sec')
        if not config.render_path:
          print(f'PSNR={psnr:.4f} SSIM={ssim:.4f}')
          summary_writer.scalar('test_psnr', psnr, step)
          summary_writer.scalar('test_ssim', ssim, step)
          summary_writer.image('test_target', test_case['rgb'], step)
        for k, v in vis_suite.items():
          summary_writer.image('test_pred_' + k, v, step)

  if config.max_steps % config.checkpoint_every != 0:
    state = jax.device_get(jax.tree_map(lambda x: x[0], state))
    checkpoints.save_checkpoint(
        config.checkpoint_dir, state, int(config.max_steps), keep=100)


if __name__ == '__main__':
  config1 = ConfigProto()
  config1.gpu_options.allow_growth = True
  session = InteractiveSession(config=config1)
  app.run(main)
