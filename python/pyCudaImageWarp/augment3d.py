import math
import numpy as np
import scipy.ndimage as nd

from pyCudaImageWarp import cudaImageWarp

"""
    Adjust the translation component of an affine transform so that it fixes
    the given point. Preserves the linear part.
"""
def fix_point_affine(mat, point):
    mat = mat.astype(float)
    mat[0:3, 3] = point - mat[0:3, 0:3].dot(point[np.newaxis].T).T
    return mat

def jitter_mask(labels, pQuit=0.5, maxIter=1, pKeep=0.5, pJagged=0.5):
    """
	Slightly modify a set of labels, with randomness. Only modifies the 
	image mask, that is, the labels less than zero. Jitters the perimeter
    """
    # With probability pQuit, do nothing at all
    if np.random.uniform() <= pQuit:
	return labels

    # Do nothing if all the labels are valid
    invalid = labels == -1
    if not np.any(invalid): 
	return labels

    # Randomly draw the number of iterations
    iters = int(round(np.random.uniform(low=1, high=maxIter)))

    # Erode or dilate smoothly
    if np.random.uniform() > pJagged:
	if np.random.uniform() > 0.5:
	    invalid = nd.morphology.binary_erosion(invalid, iterations=iters)
	else:
	    invalid = nd.morphology.binary_dilation(invalid, iterations=iters)
    else:
	# Jitter the boundary in each iteration
	for i in range(iters):

	    # Chose whether to erode or dilate
	    if np.random.uniform() > 0.5:
		new = nd.morphology.binary_erosion(invalid)
	    else:
		new = nd.morphology.binary_dilation(invalid)

	    # Get the difference and randomly choose whether to keep them
	    diff = new ^ invalid
	    invalid[diff] = np.random.uniform(size=(np.sum(diff),)) <= pKeep

    # Return the result
    result = np.zeros_like(labels)
    result[invalid] = -1
    result[~invalid] = labels[~invalid]
    return result


"""
    Randomly generates a 3D affine map based on the parameters given. Then 
    applies the map to warp the input image and, optionally, the segmentation.
    Warping is done on the GPU using pyCudaImageWarp. By default, the output
    shape is the same as that of the input image.

    By default, the function only generates the identity map. The affine
    transform distribution is controlled by the following parameters:
        imList - A list of input images.
        segList - A list of input segmentations (optional).
        shapeList - A list of output shapes (optional).
        rotMax - Uniform rotation about (x,y,z) axes. For example, (10,10,10)
            means +-10 degrees in about each axis.
        pReflect - Chance of reflecting about (x,y,z) axis. For example, 
                (.5, 0, 0) means there is a 50% chance of reflecting about the
                x-axis.
        shearMax - Uniform shearing about each axis. For example, (1.1, 1.1, 
                1.1) shears in each axis in the range (1.1, 1 / 1.1)
        transMax - Uniform translation in each coordinate. For example, (10, 10,
                10) translates by at most +-10 voxels in each coordinate.
        otherScale - Gaussian-distributed affine transform. This controls the
                variance of each parameter.
	randomCrop - If true, and the output shape differs from the input, crop
		the input uniformly at random. Otherwise, crop in the center.
        cropOffset - If provided, begin the crop at these indices. Cannot use
                this with randomCrop=True.
	noiseLevel - Decide the amount of noise using this standard deviation.
	windowMin - A pair of values, from which the lower window threshold is
		sampled uniformly. By default, this does nothing.
	windowMax - A pair of values, from which the upper window threshold is
		sampled uniformly. By default, this does nothing.
	occludeProb - Probability that we randomly take out a chunk of out of 
            the image.
	oob_label - The label assigned to out-of-bounds pixels (default: 0)
        printFun - If provided, use this function to print the parameters.
        oob_image_val - If provided, set out-of-bounds voxels to this value.
        api - Either 'cuda' or 'scipy', for testing purposes

    All transforms fix the center of the image, except for translation.
"""
def cuda_affine_augment3d(imList, segList=None, shapeList=None, rand_seed=None,
    rotMax=(0, 0, 0), pReflect=(0, 0, 0), shearMax=(1,1,1), transMax=(0,0,0),
    otherScale=0, randomCrop=False, cropOffset=None, noiseLevel=0, 
    windowMin=None, windowMax=None, occludeProb=0.0, oob_label=0, 
    printFun=None, oob_image_val=None, api='cuda'):

    # Choose the implementation based on api
    if api == 'cuda':
        pushFun = cudaImageWarp.push
        popFun = cudaImageWarp.pop
    elif api == 'scipy':
        from pyCudaImageWarp import scipyImageWarp
        pushFun = scipyImageWarp.push
        popFun = scipyImageWarp.pop
    else:
        raise ValueError('Unrecognized api: ' + api)

    # Default to have the same output and input shape
    if shapeList is None:
        shapeList = [im.shape for im in imList]

    # Verify inputs
    if segList is None and oob_label != 0:
        raise ValueError('Cannot set oob_label when segList is None')

    # Convert inputs to a list
    if segList is None:
        segList = [None for im in imList]
        haveSeg = False
    else:
        haveSeg = True
    if len(imList) != len(segList):
        raise ValueError('im and seg must have the same number of elements')

    # Create a dummy segmentation, if out-of-bounds is required
    if not haveSeg and oob_image_val is not None:
        segList = [np.zeros(shape, dtype=int) for im in imList]

    # Push all the inputs
    crops = []
    segShifts = []
    for im, seg, shape in zip(imList, segList, shapeList):

        # Shift the segmentation so the minimum label is 1, unless the OOB
        # label is zero (default) and the user doesn't specify an OOB
        # image value
        shift = 0 if oob_label == 0 and oob_image_val is None else \
                np.min(seg) - 1
        if shift != 0:
            seg -= shift

        crops.append(
            __cuda_affine_augment3d_push(im, seg, shape, rand_seed,
                rotMax, pReflect, shearMax, transMax, otherScale, randomCrop, 
                cropOffset, noiseLevel, windowMin, windowMax, occludeProb, 
                oob_label, oob_image_val, printFun, pushFun)
        )
        segShifts.append(shift)

    # Pop all the outputs
    augImList = []
    augSegList = []
    for im, seg, shape, shift in zip(imList, segList, shapeList, segShifts):
        augIm = __cuda_affine_augment3d_pop(shape, im.dtype, popFun)
        augSeg = None if seg is None else \
                __cuda_affine_augment3d_pop(shape[:3], seg.dtype, popFun)

        # Set the out-of-bounds values and undo label shifting
        if shift != 0:
            oob = augSeg == 0
            if haveSeg: # No need to undo dummy values
                augSeg += shift
                augSeg[oob] = oob_label
            if oob_image_val is not None:
                augIm[oob] = oob_image_val

        augImList.append(augIm)
        augSegList.append(augSeg)

    # Return two or three outputs, depending on the input
    return augImList, augSegList, crops if haveSeg else augImList, crops

def __cuda_affine_augment3d_push(im, seg, shape, rand_seed, rotMax, pReflect, 
        shearMax, transMax, otherScale, randomCrop, cropOffset, noiseLevel, 
        windowMin, windowMax, occludeProb, oob_label, oob_image_val, printFun, 
        pushFun):
    """
        Start processing an image. Called by cuda_affine_augment3d. Returns the
        cropping coordinates. Pushes im first, then pushes seg if it's not None.
    """

    # Pad the image to have a channel dimension
    ndim = 3
    if len(im.shape) < ndim + 1:
	im = np.expand_dims(im, ndim)

    # Pad the shape with a channel dimension
    if len(shape) == ndim:
	shape = shape + (1,)

    # Check that the input and output shapes are compatible
    if len(shape) > ndim and shape[ndim] != im.shape[ndim]:
	raise ValueError("Output shape has %d channels, while input has %d" % \
		(shape[3], im.shape[3]))
    if len(shape) != len(im.shape):
	raise ValueError("""
		Input and output shapes have mismatched number of dimensions.
		Input: %s, Output: %s"
		""" % (shape, im.shape))

    #  Set the random seed, if specified
    if rand_seed is not None:
        np.random.seed(rand_seed)

    # ---Randomly generate the desired transforms, in homogeneous coordinates---
    
    # Draw the noise level
    noiseScale = np.abs(np.random.normal(scale=noiseLevel))

    # Draw the width of occlusion, if any
    if np.random.uniform() < occludeProb:
        occludeWidth = int(np.floor(np.random.uniform(low=0, 
                high=im.shape[2] / 2)))
    else:
        occludeWidth = None

    # Cropping translation
    if cropOffset is not None:

        if randomCrop:
            raise ValueError("Cannot use both randomCrop and cropOffset!")

        # Use the user-provided crop
        crop_start = cropOffset
    else:
        # Generate a crop based on the inputs
        crop_range = np.maximum(np.array(im.shape[:3]) - np.array(shape[:3]), 0)
        if np.equal(im.shape, shape).all():
            crop_start = np.zeros_like(im.shape[:3])
        elif randomCrop:
            crop_start = np.random.uniform(high=crop_range)
        else:
            crop_start = crop_range / 2 # Center crop
    mat_crop = np.identity(4)
    mat_crop[0:3, 3] = crop_start

    # Get the center of the crop, to make sure our other transformations fix it
    crop_center = crop_start + np.array(shape)[:-1] / 2.0

    # Uniform rotation
    rotate_deg = np.random.uniform(low=-np.array(rotMax), high=rotMax)
    #rotate_deg = np.random.normal(scale=np.array(rotMax) / 4)
    lin_rotate = np.identity(3)
    for i in range(3): # Rotate about each axis and combine
        # Compute the angle of rotation, in radians
        rad = rotate_deg[i] * 2 * math.pi / 360

        # Form the rotation matrix about this axis
        rot = np.identity(3)
        axes = [x for x in range(3) if x != i]
        rot[axes[0], axes[0]] = math.cos(rad)
        rot[axes[0], axes[1]] = -math.sin(rad)
        rot[axes[1], axes[0]] = -rot[axes[0], axes[1]]
        rot[axes[1], axes[1]] = rot[axes[0], axes[0]]

        # Compose all the rotations
        lin_rotate = lin_rotate.dot(rot)

    # Extend the linear rotation to an affine transform
    mat_rotate = np.identity(4)
    mat_rotate[0:3, 0:3] = lin_rotate
    mat_rotate = fix_point_affine(mat_rotate, crop_center)

    # Uniform shear, same chance of shrinking and growing
    if np.any(shearMax <= 0):
        raise ValueError("Invalid shearMax: %f" % (shear))    
    #shear = np.random.uniform(low=1.0, high=shearMax, size=3)
    shear = np.random.normal(loc=1.0, scale=np.array(shearMax) / 4, size=3)
    invert_shear = np.random.uniform(size=3) < 0.5
    shear[invert_shear] = 1.0 / shear[invert_shear]
    mat_shear = fix_point_affine(np.diag(np.hstack((shear, 1))), crop_center)

    # Reflection
    do_reflect = np.random.uniform(size=3) < pReflect
    mat_reflect = fix_point_affine(np.diag(np.hstack((1 - 2 * do_reflect, 1))),
        crop_center)

    # Generic affine transform, Gaussian-distributed
    mat_other = np.identity(4)
    mat_other[0:3, :] = mat_other[0:3, :] + \
        np.random.normal(loc=0.0, scale=otherScale, size=(3,4))
    mat_other = fix_point_affine(mat_other, crop_center) 

    # Uniform translation
    translation = np.random.uniform(low=-np.array(transMax), 
            high=transMax)
    mat_translate = np.identity(4)
    mat_translate[0:3, 3] = translation

    # Compose all the transforms
    warp_affine = (
        mat_crop.dot( mat_translate.dot( mat_rotate.dot( mat_shear.dot( 
		mat_reflect.dot(mat_other)
	))))
    )[0:3, :]

    # Draw the window thresholds uniformly in the specified range
    if windowMin is not None:
	winMin = np.random.uniform(low=windowMin[0], high=windowMin[1])
    else:
	winMin = -float('inf')
    if windowMax is not None:
	winMax = np.random.uniform(low=windowMax[0], high=windowMax[1])
    else:
	winMax = float('inf')

    # Draw the occlusion parameters
    if occludeWidth is not None:
	# Take a chunk out at random	
	occZmin = int(np.floor(np.random.uniform(
		low=-occludeWidth, high=im.shape[2])))
	occZmax = occZmin + occludeWidth - 1
    else:
	# By default, do no occlusion
	occZmin = 0
	occZmax = -1

    # Warp each image channel the same way
    for c in range(shape[3]):
	pushFun(
		im[:, :, :, c], 
		warp_affine, 
		interp='linear',
		shape=shape[:3],
		std=noiseScale,
		winMin=winMin,
		winMax=winMax,
		occZmin=occZmin,
		occZmax=occZmax
	)

    # Optionally print the result
    if printFun is not None:
        printFun("occZmin: %d occZmax: %d" % (occZmin, occZmax))
        printFun("winZmin: %d winZmax: %d" % (winMin, winMax))
        printFun("rotation: [%d, %d, %d]" % (rotate_deg[0], rotate_deg[1], 
                rotate_deg[2]))
        printFun("translation: [%d, %d, %d]" % (translation[0], translation[1],
                translation[2]))

    # Return early if there's no segmentation
    if seg is None:
        return crop_start

    # Warp the segmentation
    pushFun(
	seg, 
	warp_affine, 
	interp='nearest',
	shape=shape[:3], 
	occZmin=occZmin,
	occZmax=occZmax
    )

    return crop_start

def __cuda_affine_augment3d_pop(shape, dtype, popFun):
    """
    Finish processing an image, and return the result. Squeezes out the channel
    dimension, if necessary.
    """
    # Pop multi-channel images one channel at a time
    if len(shape) > 3:
        im = np.zeros(shape, dtype=dtype, order='F')
        for c in range(im.shape[3]):
            im[:, :, :, c] = popFun()
        return im

    # Pop a single-channel image
    return popFun()