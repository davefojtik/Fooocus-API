import copy
import random
import time
import numpy as np
import torch
from typing import List
from fooocusapi.api_utils import read_input_image
from fooocusapi.models import GeneratedImage, GenerationFinishReason, ImgInpaintOrOutpaintRequest, ImgUpscaleOrVaryRequest, PerfomanceSelection, TaskType, Text2ImgRequest
from fooocusapi.task_queue import TaskQueue
from modules.expansion import safe_str
from modules.path import downloading_inpaint_models
from modules.sdxl_styles import apply_style, fooocus_expansion, aspect_ratios

task_queue = TaskQueue()


def process_generate(req: Text2ImgRequest) -> List[GeneratedImage]:
    import modules.default_pipeline as pipeline
    import modules.patch as patch
    import modules.flags as flags
    import modules.core as core
    import modules.inpaint_worker as inpaint_worker
    import comfy.model_management as model_management
    from modules.util import join_prompts, remove_empty_str, image_is_generated_in_current_ui, resize_image
    from modules.private_logger import log
    from modules.upscaler import perform_upscale

    task_seq = task_queue.add_task(TaskType.text2img, {
        'body': req.__dict__})
    if task_seq is None:
        print("[Task Queue] The task queue has reached limit")
        results: List[GeneratedImage] = []
        for i in range(0, req.image_number):
            results.append(GeneratedImage(im=None, seed=0,
                           finish_reason=GenerationFinishReason.queue_is_full))
        return results

    try:
        waiting_sleep_steps: int = 0
        waiting_start_time = time.perf_counter()
        while not task_queue.is_task_ready_to_start(task_seq):
            if waiting_sleep_steps == 0:
                print(
                    f"[Task Queue] Waiting for task queue become free, seq={task_seq}")
            delay = 0.1
            time.sleep(delay)
            waiting_sleep_steps += 1
            if waiting_sleep_steps % int(10 / delay) == 0:
                waiting_time = time.perf_counter() - waiting_start_time
                print(
                    f"[Task Queue] Already waiting for {waiting_time}S, seq={task_seq}")

        print(f"[Task Queue] Task queue is free, start task, seq={task_seq}")

        task_queue.start_task(task_seq)

        execution_start_time = time.perf_counter()

        loras = [(l.model_name, l.weight) for l in req.loras]
        loras_user_raw_input = copy.deepcopy(loras)

        style_selections = [s.value for s in req.style_selections]
        raw_style_selections = copy.deepcopy(style_selections)
        if fooocus_expansion in style_selections:
            use_expansion = True
            style_selections.remove(fooocus_expansion)
        else:
            use_expansion = False

        use_style = len(req. style_selections) > 0

        adaptive_cfg = 7
        patch.adaptive_cfg = adaptive_cfg
        print(f'[Parameters] Adaptive CFG = {patch.adaptive_cfg}')

        patch.sharpness = req.sharpness
        print(f'[Parameters] Sharpness = {patch.sharpness}')

        adm_scaler_positive = 1.5
        adm_scaler_negative = 0.8
        adm_scaler_end = 0.3
        patch.positive_adm_scale = adm_scaler_positive
        patch.negative_adm_scale = adm_scaler_negative
        patch.adm_scaler_end = adm_scaler_end
        print(
            f'[Parameters] ADM Scale = {patch.positive_adm_scale} : {patch.negative_adm_scale} : {patch.adm_scaler_end}')

        cfg_scale = req.guidance_scale
        print(f'[Parameters] CFG = {cfg_scale}')

        initial_latent = None
        denoising_strength = 1.0
        tiled = False
        results: List[GeneratedImage] = []

        if req.performance_selection == PerfomanceSelection.speed:
            steps = 30
            switch = 20
        else:
            steps = 60
            switch = 40

        pipeline.clear_all_caches()
        width, height = aspect_ratios[req.aspect_ratios_selection.value]

        sampler_name = flags.default_sampler
        scheduler_name = flags.default_scheduler

        if isinstance(req, ImgUpscaleOrVaryRequest):
            uov_method = req.uov_method.value.lower()
            uov_input_image = read_input_image(req.input_image)
            if 'vary' in uov_method:
                if not image_is_generated_in_current_ui(uov_input_image, ui_width=width, ui_height=height):
                    uov_input_image = resize_image(
                        uov_input_image, width=width, height=height)
                    print(
                        f'Resolution corrected - users are uploading their own images.')
                else:
                    print(f'Processing images generated by Fooocus.')
                if 'subtle' in uov_method:
                    denoising_strength = 0.5
                if 'strong' in uov_method:
                    denoising_strength = 0.85
                initial_pixels = core.numpy_to_pytorch(uov_input_image)
                initial_latent = core.encode_vae(
                    vae=pipeline.xl_base_patched.vae, pixels=initial_pixels)
                B, C, H, W = initial_latent['samples'].shape
                width = W * 8
                height = H * 8
                print(f'Final resolution is {str((height, width))}.')
            elif 'upscale' in uov_method:
                H, W, C = uov_input_image.shape

                uov_input_image = core.numpy_to_pytorch(uov_input_image)
                uov_input_image = perform_upscale(uov_input_image)
                uov_input_image = core.pytorch_to_numpy(uov_input_image)[0]
                print(f'Image upscaled.')

                if '1.5x' in uov_method:
                    f = 1.5
                elif '2x' in uov_method:
                    f = 2.0
                else:
                    f = 1.0

                width_f = int(width * f)
                height_f = int(height * f)

                if image_is_generated_in_current_ui(uov_input_image, ui_width=width_f, ui_height=height_f):
                    uov_input_image = resize_image(
                        uov_input_image, width=int(W * f), height=int(H * f))
                    print(f'Processing images generated by Fooocus.')
                else:
                    uov_input_image = resize_image(
                        uov_input_image, width=width_f, height=height_f)
                    print(
                        f'Resolution corrected - users are uploading their own images.')

                H, W, C = uov_input_image.shape
                image_is_super_large = H * W > 2800 * 2800

                if 'fast' in uov_method:
                    direct_return = True
                elif image_is_super_large:
                    print('Image is too large. Directly returned the SR image. '
                          'Usually directly return SR image at 4K resolution '
                          'yields better results than SDXL diffusion.')
                    direct_return = True
                else:
                    direct_return = False

                if direct_return:
                    d = [('Upscale (Fast)', '2x')]
                    log(uov_input_image, d, single_line_number=1)
                    for i in range(0, req.image_number):
                        results.append(GeneratedImage(
                            im=uov_input_image, seed=0, finish_reason=GenerationFinishReason.success))
                    print(f"[Task Queue] Finish task, seq={task_seq}")
                    task_queue.finish_task(task_seq, results, False)
                    return results

                tiled = True
                denoising_strength = 1.0 - 0.618
                steps = int(steps * 0.618)
                switch = int(steps * 0.67)

                initial_pixels = core.numpy_to_pytorch(uov_input_image)

                initial_latent = core.encode_vae(
                    vae=pipeline.xl_base_patched.vae, pixels=initial_pixels, tiled=True)
                B, C, H, W = initial_latent['samples'].shape
                width = W * 8
                height = H * 8
                print(f'Final resolution is {str((height, width))}.')

        elif isinstance(req, ImgInpaintOrOutpaintRequest):
            inpaint_image = read_input_image(req.input_image)
            if req.input_mask is not None:
                inpaint_mask = read_input_image(
                    req.input_mask)[:, :, 0]
            else:
                inpaint_mask = np.zeros(inpaint_image.shape[:-1])
            outpaint_selections = [s.value.lower()
                                   for s in req.outpaint_selections]
            if isinstance(inpaint_image, np.ndarray) and isinstance(inpaint_mask, np.ndarray) \
                    and (np.any(inpaint_mask > 127) or len(outpaint_selections) > 0):
                if len(outpaint_selections) > 0:
                    H, W, C = inpaint_image.shape
                    if 'top' in outpaint_selections:
                        inpaint_image = np.pad(
                            inpaint_image, [[int(H * 0.3), 0], [0, 0], [0, 0]], mode='edge')
                        inpaint_mask = np.pad(
                            inpaint_mask, [[int(H * 0.3), 0], [0, 0]], mode='constant', constant_values=255)
                    if 'bottom' in outpaint_selections:
                        inpaint_image = np.pad(
                            inpaint_image, [[0, int(H * 0.3)], [0, 0], [0, 0]], mode='edge')
                        inpaint_mask = np.pad(
                            inpaint_mask, [[0, int(H * 0.3)], [0, 0]], mode='constant', constant_values=255)

                    H, W, C = inpaint_image.shape
                    if 'left' in outpaint_selections:
                        inpaint_image = np.pad(
                            inpaint_image, [[0, 0], [int(H * 0.3), 0], [0, 0]], mode='edge')
                        inpaint_mask = np.pad(
                            inpaint_mask, [[0, 0], [int(H * 0.3), 0]], mode='constant', constant_values=255)
                    if 'right' in outpaint_selections:
                        inpaint_image = np.pad(
                            inpaint_image, [[0, 0], [0, int(H * 0.3)], [0, 0]], mode='edge')
                        inpaint_mask = np.pad(inpaint_mask, [[0, 0], [0, int(
                            H * 0.3)]], mode='constant', constant_values=255)

                    inpaint_image = np.ascontiguousarray(inpaint_image.copy())
                    inpaint_mask = np.ascontiguousarray(inpaint_mask.copy())

                inpaint_worker.current_task = inpaint_worker.InpaintWorker(image=inpaint_image, mask=inpaint_mask,
                                                                           is_outpaint=len(outpaint_selections) > 0)

                # print(f'Inpaint task: {str((height, width))}')
                # outputs.append(['results', inpaint_worker.current_task.visualize_mask_processing()])
                # return

                inpaint_head_model_path, inpaint_patch_model_path = downloading_inpaint_models()
                loras += [(inpaint_patch_model_path, 1.0)]

                inpaint_pixels = core.numpy_to_pytorch(
                    inpaint_worker.current_task.image_ready)
                initial_latent = core.encode_vae(
                    vae=pipeline.xl_base_patched.vae, pixels=inpaint_pixels)
                inpaint_latent = initial_latent['samples']
                B, C, H, W = inpaint_latent.shape
                inpaint_mask = core.numpy_to_pytorch(
                    inpaint_worker.current_task.mask_ready[None])
                inpaint_mask = torch.nn.functional.avg_pool2d(
                    inpaint_mask, (8, 8))
                inpaint_mask = torch.nn.functional.interpolate(
                    inpaint_mask, (H, W), mode='bilinear')
                inpaint_worker.current_task.load_latent(
                    latent=inpaint_latent, mask=inpaint_mask)

                inpaint_mask = (
                    inpaint_worker.current_task.mask_ready > 0).astype(np.float32)
                inpaint_mask = torch.tensor(inpaint_mask).float()

                vae_dict = core.encode_vae_inpaint(
                    mask=inpaint_mask, vae=pipeline.xl_base_patched.vae, pixels=inpaint_pixels)

                inpaint_latent = vae_dict['samples']
                inpaint_mask = vae_dict['noise_mask']
                inpaint_worker.current_task.load_inpaint_guidance(
                    latent=inpaint_latent, mask=inpaint_mask, model_path=inpaint_head_model_path)

                B, C, H, W = inpaint_latent.shape
                height, width = inpaint_worker.current_task.image_raw.shape[:2]
                print(
                    f'Final resolution is {str((height, width))}, latent is {str((H * 8, W * 8))}.')

                sampler_name = 'dpmpp_fooocus_2m_sde_inpaint_seamless'

        print(f'[Parameters] Sampler = {sampler_name} - {scheduler_name}')

        raw_prompt = req.prompt
        raw_negative_prompt = req.negative_promit

        prompts = remove_empty_str([safe_str(p)
                                    for p in req.prompt.split('\n')], default='')
        negative_prompts = remove_empty_str(
            [safe_str(p) for p in req.negative_promit.split('\n')], default='')

        prompt = prompts[0]
        negative_prompt = negative_prompts[0]

        extra_positive_prompts = prompts[1:] if len(prompts) > 1 else []
        extra_negative_prompts = negative_prompts[1:] if len(
            negative_prompts) > 1 else []

        seed = req.image_seed
        max_seed = int(1024 * 1024 * 1024)
        if not isinstance(seed, int):
            seed = random.randint(1, max_seed)
        if seed < 0:
            seed = - seed
        seed = seed % max_seed

        pipeline.refresh_everything(
            refiner_model_name=req.refiner_model_name,
            base_model_name=req.base_model_name,
            loras=loras
        )
        pipeline.prepare_text_encoder(async_call=False)

        positive_basic_workloads = []
        negative_basic_workloads = []

        if use_style:
            for s in style_selections:
                p, n = apply_style(s, positive=prompt)
                positive_basic_workloads.append(p)
                negative_basic_workloads.append(n)
        else:
            positive_basic_workloads.append(prompt)

        positive_basic_workloads = positive_basic_workloads + extra_positive_prompts
        negative_basic_workloads = negative_basic_workloads + extra_negative_prompts

        positive_basic_workloads = remove_empty_str(
            positive_basic_workloads, default=prompt)
        negative_basic_workloads = remove_empty_str(
            negative_basic_workloads, default=negative_prompt)

        positive_top_k = len(positive_basic_workloads)
        negative_top_k = len(negative_basic_workloads)

        tasks = [dict(
            task_seed=seed + i,
            positive=positive_basic_workloads,
            negative=negative_basic_workloads,
            expansion='',
            c=[None, None],
            uc=[None, None],
        ) for i in range(req.image_number)]

        if use_expansion:
            for i, t in enumerate(tasks):
                expansion = pipeline.expansion(prompt, t['task_seed'])
                print(f'[Prompt Expansion] New suffix: {expansion}')
                t['expansion'] = expansion
                # Deep copy.
                t['positive'] = copy.deepcopy(
                    t['positive']) + [join_prompts(prompt, expansion)]

        for i, t in enumerate(tasks):
            t['c'][0] = pipeline.clip_encode(sd=pipeline.xl_base_patched, texts=t['positive'],
                                             pool_top_k=positive_top_k)

        for i, t in enumerate(tasks):
            t['uc'][0] = pipeline.clip_encode(sd=pipeline.xl_base_patched, texts=t['negative'],
                                              pool_top_k=negative_top_k)

        if pipeline.xl_refiner is not None:
            for i, t in enumerate(tasks):
                t['c'][1] = pipeline.clip_separate(t['c'][0])

            for i, t in enumerate(tasks):
                t['uc'][1] = pipeline.clip_separate(t['uc'][0])

        all_steps = steps * req.image_number

        def callback(step, x0, x, total_steps, y):
            done_steps = current_task_id * steps + step
            print(f"Finished {done_steps}/{all_steps}")

        preparation_time = time.perf_counter() - execution_start_time
        print(f'Preparation time: {preparation_time:.2f} seconds')

        process_with_error = False
        for current_task_id, task in enumerate(tasks):
            execution_start_time = time.perf_counter()

            try:
                imgs = pipeline.process_diffusion(
                    positive_cond=task['c'],
                    negative_cond=task['uc'],
                    steps=steps,
                    switch=switch,
                    width=width,
                    height=height,
                    image_seed=task['task_seed'],
                    callback=callback,
                    sampler_name=sampler_name,
                    scheduler_name=scheduler_name,
                    latent=initial_latent,
                    denoise=denoising_strength,
                    tiled=tiled,
                    cfg_scale=cfg_scale
                )

                for x in imgs:
                    d = [
                        ('Prompt', raw_prompt),
                        ('Negative Prompt', raw_negative_prompt),
                        ('Fooocus V2 Expansion', task['expansion']),
                        ('Styles', str(raw_style_selections)),
                        ('Performance', req.performance_selection),
                        ('Resolution', str((width, height))),
                        ('Sharpness', req.sharpness),
                        ('Guidance Scale', req.guidance_scale),
                        ('ADM Guidance', str((adm_scaler_positive, adm_scaler_negative))),
                        ('Base Model', req.base_model_name),
                        ('Refiner Model', req.refiner_model_name),
                        ('Sampler', sampler_name),
                        ('Scheduler', scheduler_name),
                        ('Seed', task['task_seed'])
                    ]
                    for n, w in loras_user_raw_input:
                        if n != 'None':
                            d.append((f'LoRA [{n}] weight', w))
                    log(x, d, single_line_number=3)

                results.append(GeneratedImage(
                    im=imgs[0], seed=task['task_seed'], finish_reason=GenerationFinishReason.success))
            except model_management.InterruptProcessingException as e:
                print('User stopped')
                for i in range(current_task_id + 1, len(tasks)):
                    results.append(GeneratedImage(
                        im=None, seed=task['task_seed'], finish_reason=GenerationFinishReason.user_cancel))
                break
            except Exception as e:
                print('Process failed:', e)
                process_with_error = True
                results.append(GeneratedImage(
                    im=None, seed=task['task_seed'], finish_reason=GenerationFinishReason.error))

            execution_time = time.perf_counter() - execution_start_time
            print(f'Generating and saving time: {execution_time:.2f} seconds')

        pipeline.prepare_text_encoder(async_call=True)

        print(f"[Task Queue] Finish task, seq={task_seq}")
        task_queue.finish_task(task_seq, results, process_with_error)

        return results
    except Exception as e:
        print('Worker error:', e)
        print(f"[Task Queue] Finish task, seq={task_seq}")
        task_queue.finish_task(task_seq, [], True)
        raise e
